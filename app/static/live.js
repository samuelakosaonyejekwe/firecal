/* FireCal live fires straight from NASA, for when the published copy falls behind.
 *
 * The website normally shows live fires that GitHub republishes within minutes of each NASA update
 * (pipeline/live.py). If that copy is more than an hour behind NASA (e.g. GitHub Actions is down),
 * the visitor's browser reads NASA's own 7-day file from the FIRMS mirror that allows browser access
 * (firms2) and builds the same live data here, with the same rules as pipeline/live.py:
 * nominal/high confidence, 0.1° cells, industrial-heat cells masked, rolling-window edge days partial.
 * tests/test_live_parity.py checks this file against pipeline/live.py on the same feed.
 *
 * In a page it parses in a Web Worker (the 7-day file is ~30 MB) so the app stays responsive.
 */
(function (root) {
  "use strict";
  const CELL = 10, MAX_OVERVIEW = 15000;
  const FEED = "https://firms2.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_7d.csv";
  const KEEP = new Set(["n", "h", "nominal", "high"]);
  const key = (yi, xi) => (yi + 1000) * 4000 + (xi + 2000); // one integer per 0.1° cell

  /** CSV text -> cell-days {days, di, yi, xi, n} (typed arrays), aggregated like live.py's fetch_cells + mask. */
  function parse(text, staticCells) {
    const masked = new Set();
    for (let i = 0; i + 1 < (staticCells || []).length; i += 2) masked.add(key(staticCells[i], staticCells[i + 1]));
    let pos = text.indexOf("\n");
    const head = text.slice(0, pos).trim().split(",");
    const iLat = head.indexOf("latitude"), iLon = head.indexOf("longitude"), iDate = head.indexOf("acq_date"), iConf = head.indexOf("confidence");
    if (iLat < 0 || iLon < 0 || iDate < 0 || iConf < 0) throw new Error("unexpected NASA file format");
    const agg = new Map(), dayIx = new Map();
    while (pos >= 0 && pos < text.length) {
      const end = text.indexOf("\n", pos + 1), line = text.slice(pos + 1, end < 0 ? text.length : end);
      pos = end;
      if (!line) continue;
      const f = line.split(",");
      if (!KEEP.has(f[iConf]?.trim())) continue;
      const yi = Math.floor(+f[iLat] * CELL), xi = Math.floor(+f[iLon] * CELL);
      if (!Number.isFinite(yi) || !Number.isFinite(xi) || masked.has(key(yi, xi))) continue;
      const d = f[iDate];
      if (!dayIx.has(d)) dayIx.set(d, dayIx.size);
      const k = dayIx.get(d) * 1e8 + key(yi, xi);
      agg.set(k, (agg.get(k) || 0) + 1);
    }
    const raw = [...dayIx.keys()], days = [...raw].sort(), remap = raw.map((d) => days.indexOf(d));
    const n = agg.size, out = { days, di: new Uint8Array(n), yi: new Int16Array(n), xi: new Int16Array(n), n: new Uint32Array(n) };
    let i = 0;
    for (const [k, c] of agg) {
      const cell = k % 1e8;
      out.di[i] = remap[Math.floor(k / 1e8)];
      out.yi[i] = Math.floor(cell / 4000) - 1000;
      out.xi[i] = (cell % 4000) - 2000;
      out.n[i++] = c;
    }
    return out;
  }

  function quantile(sorted, q) { // numpy's default (linear) quantile
    if (!sorted.length) return 0;
    const h = (sorted.length - 1) * q, lo = Math.floor(h);
    return sorted[lo] + (h - lo) * ((sorted[Math.min(lo + 1, sorted.length - 1)]) - sorted[lo]);
  }

  /** Cell-days -> the same structures pipeline/live.py publishes (meta, overview, tile index + tiles). */
  function build(cd, tileCells, sourceModified) {
    const N = cd.n.length, days = cd.days;
    const sums = new Map();
    for (let i = 0; i < N; i++) { const k = key(cd.yi[i], cd.xi[i]); sums.set(k, (sums.get(k) || 0) + cd.n[i]); }
    let f = 1, ov = sums;
    while (ov.size > MAX_OVERVIEW) {
      f += 1; ov = new Map();
      for (const [k, v] of sums) {
        const yi = Math.floor(k / 4000) - 1000, xi = (k % 4000) - 2000, kk = key(Math.floor(yi / f), Math.floor(xi / f));
        ov.set(kk, (ov.get(kk) || 0) + v);
      }
    }
    const ovCells = [...ov].map(([k, v]) => [(k % 4000) - 2000, Math.floor(k / 4000) - 1000, v]);
    const vals = ovCells.map((c) => c[2]).sort((a, b) => a - b);
    const overview = { cell: f / CELL, max: Math.round(quantile(vals, 0.99) * 100) / 100, days, cells: ovCells };
    const tiles = {}, index = { tile_cells: tileCells, tiles: {} };
    for (let i = 0; i < N; i++) {
      const t = `${Math.floor(cd.yi[i] / tileCells)}_${Math.floor(cd.xi[i] / tileCells)}`;
      (tiles[t] ||= { days, rows: [] }).rows.push([cd.di[i], cd.xi[i], cd.yi[i], cd.n[i]]);
    }
    for (const t in tiles) index.tiles[t] = tiles[t].rows.length;
    const meta = { source_last_modified: sourceModified, days, complete_days: days.slice(1, -1),
                   fetched_at: new Date().toISOString(), detections: cd.n.reduce((a, b) => a + b, 0), direct_from_nasa: true };
    return { meta, overview, index, tiles };
  }

  /** Fire cell-days per complete day in one country, like live.py's countries.json: cells whose centre
   *  is inside its border (as geo.js) or in its own recorded cells outside the border (own_cells.json). */
  function countryCells(live, feature, ownCells, geo = root.FireGeo) {
    const days = live.meta.complete_days, pos = new Map(days.map((d, i) => [d, i])), cells = days.map(() => 0), seen = new Map();
    const own = new Set();
    for (let i = 0; i + 1 < (ownCells || []).length; i += 2) own.add(key(ownCells[i], ownCells[i + 1]));
    const inside = (xi, yi) => {
      const k = key(yi, xi);
      if (!seen.has(k)) seen.set(k, own.has(k) || (!!feature && geo.pointTouches(feature, (xi + 0.5) / CELL, (yi + 0.5) / CELL)));
      return seen.get(k);
    };
    for (const t of Object.values(live.tiles))
      for (const [di, xi, yi] of t.rows) { const p = pos.get(t.days[di]); if (p != null && inside(xi, yi)) cells[p] += 1; }
    return cells;
  }

  /** In a page: download + parse NASA's file in a worker (falls back to the main thread). */
  const SELF_URL = typeof document !== "undefined" && document.currentScript ? document.currentScript.src : null;
  async function fetchParsed(staticCells) {
    if (SELF_URL && typeof Worker !== "undefined") {
      try {
        return await new Promise((resolve, reject) => {
          const w = new Worker(SELF_URL);
          w.onmessage = (e) => { w.terminate(); e.data.error ? reject(new Error(e.data.error)) : resolve(e.data); };
          w.onerror = (e) => { w.terminate(); reject(new Error(e.message || "worker failed")); };
          w.postMessage({ url: FEED, staticCells });
        });
      } catch (e) { /* e.g. workers blocked: parse here instead */ }
    }
    const r = await fetch(FEED, { cache: "no-store" });
    if (!r.ok) throw new Error(`NASA returned HTTP ${r.status}`);
    return parse(await r.text(), staticCells);
  }

  async function nasaModified(timeoutMs = 8000) { // when NASA last updated the feed (a few hundred bytes)
    const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const r = await fetch(FEED, { method: "HEAD", cache: "no-store", signal: ctl.signal });
      const lm = r.ok && r.headers.get("Last-Modified");
      return lm ? new Date(lm) : null;
    } catch (e) { return null; } finally { clearTimeout(t); }
  }

  const api = { FEED, parse, build, countryCells, quantile, fetchParsed, nasaModified };
  if (typeof WorkerGlobalScope !== "undefined" && root instanceof WorkerGlobalScope) {
    root.onmessage = async (e) => {
      try {
        const r = await fetch(e.data.url, { cache: "no-store" });
        if (!r.ok) throw new Error(`NASA returned HTTP ${r.status}`);
        const out = parse(await r.text(), e.data.staticCells);
        root.postMessage(out, [out.di.buffer, out.yi.buffer, out.xi.buffer, out.n.buffer]);
      } catch (err) { root.postMessage({ error: String(err.message || err) }); }
    };
  } else if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FireLive = api;
})(typeof self !== "undefined" ? self : this);
