/* FireCal data layer: one interface, two backends.
 *   Server edition  -> the FastAPI endpoints (api/…)
 *   Static edition  -> precomputed files on GitHub Pages (data/…) + engine.js for drawn boxes
 * All paths are relative so the app works at a site root or under a sub-path (/firecal/).
 */
(function () {
  "use strict";
  const STATIC = !!window.FIRECAL_STATIC;
  const CELL = 10;

  class HttpError extends Error {
    constructor(status, body) { super(body?.detail || `HTTP ${status}`); this.status = status; this.body = body; }
  }
  async function fetchJSON(url, opts = {}, tries = 3) {
    for (let i = 0; ; i++) {
      try {
        const r = await fetch(url, opts);
        const body = await r.json().catch(() => null);
        if (!r.ok) throw new HttpError(r.status, body);
        return body;
      } catch (e) {
        if (i + 1 >= tries || (e instanceof HttpError && e.status < 500)) throw e;
        await new Promise((res) => setTimeout(res, 600 * 2 ** i));
      }
    }
  }
  const cellBounds = ([w, s, e, n]) => ({ y0: Math.floor(s * CELL), y1: Math.ceil(n * CELL) - 1, x0: Math.floor(w * CELL), x1: Math.ceil(e * CELL) - 1 });
  const aoiQuery = (a) => (a.country ? `country=${encodeURIComponent(a.country)}` : `bbox=${a.bbox.join(",")}`);

  // ───────────────────────── server edition ─────────────────────────
  const Server = {
    mode: "server",
    async meta() { const m = await fetchJSON("api/meta"); this.maxBoxDeg2 = m.max_box_deg2; return m; },
    calendar: (a) => fetchJSON(`api/calendar?${aoiQuery(a)}`),
    nowcast: (a) => fetchJSON(`api/nowcast?${aoiQuery(a)}`, {}, 2),
    grid: (bbox, year, month) => {
      const q = new URLSearchParams({ bbox: bbox.join(",") });
      if (year) q.set("year", year);
      if (month) q.set("month", month);
      return fetchJSON(`api/grid?${q}`);
    },
    live: (bbox) => fetchJSON(`api/live?bbox=${bbox.join(",")}`),
    locate: (lon, lat) => fetchJSON(`api/locate?lon=${lon}&lat=${lat}`),
    prepare: (ids) => fetchJSON("api/prepare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(ids) }, 2),
    jobs: () => fetchJSON("api/jobs", {}, 2),
    maxBoxDeg2: Infinity, // set from the server's meta
  };

  // ───────────────────────── static edition ─────────────────────────
  const cache = new Map();
  const once = (key, fn) => { if (!cache.has(key)) cache.set(key, fn().catch((e) => { cache.delete(key); throw e; })); return cache.get(key); };
  let META = null, WORLD = null;

  async function gunzip(url) {
    const r = await fetch(url);
    if (!r.ok) throw new HttpError(r.status, null);
    const buf = new Uint8Array(await r.arrayBuffer());
    if (buf[0] !== 0x1f || buf[1] !== 0x8b) return buf.buffer; // already decoded by the server
    if (!("DecompressionStream" in window)) throw new Error("this browser can't decompress tiles; please update it");
    const ds = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
    return new Response(ds).arrayBuffer();
  }

  async function pool(items, n, fn) { // run fn over items with n in flight
    const out = new Array(items.length); let i = 0;
    await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => { while (i < items.length) { const k = i++; out[k] = await fn(items[k], k); } }));
    return out;
  }

  // country borders for boxes and "◎ Me": the server's own file (1:50m), loaded only when needed
  const shapes = () => once("shapes", () => fetchJSON("shapes.geojson"));
  async function countriesTouching(bbox) {
    const ids = (await shapes()).features.filter((f) => FireGeo.boxTouches(f, bbox)).map((f) => f.properties.id);
    const [w, s, e, n] = bbox; // shapeless territories (e.g. Guadeloupe): by data extent, as on the server
    for (const c of META.countries) if (c.extent && !(c.extent[2] < w || c.extent[0] > e || c.extent[3] < s || c.extent[1] > n)) ids.push(c.id);
    return [...new Set(ids)].sort();
  }

  async function boxSeries(bbox, onProgress) {
    const idx = await once("tiles", () => fetchJSON("data/tiles/index.json"));
    const { y0, y1, x0, x1 } = cellBounds(bbox);
    const T = idx.tile_cells, wanted = [];
    for (let ty = Math.floor(y0 / T); ty <= Math.floor(y1 / T); ty++)
      for (let tx = Math.floor(x0 / T); tx <= Math.floor(x1 / T); tx++)
        if (idx.tiles[`${ty}_${tx}`] != null) wanted.push([ty, tx]);
    const total = wanted.reduce((s, [ty, tx]) => s + idx.tiles[`${ty}_${tx}`], 0);
    const endYear = +META.range.end.slice(0, 4);
    const S = FireEngine.series(endYear);
    const arrs = [S.m, S.v, S.t];
    let done = 0, bytes = 0;
    onProgress?.(0, wanted.length, 0, total);
    await pool(wanted, 6, async ([ty, tx]) => {
      const buf = await once(`tile:${ty}_${tx}`, () => gunzip(`data/tiles/${ty}_${tx}.bin.gz`));
      const dv = new DataView(buf);
      const counts = [dv.getUint32(8, true), dv.getUint32(12, true), dv.getUint32(16, true)];
      let off = 20;
      for (let s = 0; s < 3; s++) {
        const n = counts[s], days = new Uint16Array(buf, off, n), cells = new Uint16Array(buf, off + 2 * n, n);
        off += 4 * n;
        const arr = arrs[s];
        let day = 0;
        for (let i = 0; i < n; i++) {
          day += days[i];
          const yi = ty * T + Math.floor(cells[i] / T), xi = tx * T + (cells[i] % T);
          if (yi >= y0 && yi <= y1 && xi >= x0 && xi <= x1 && day < arr.length) arr[day] += 1;
        }
      }
      done++; bytes += idx.tiles[`${ty}_${tx}`];
      onProgress?.(done, wanted.length, bytes, total);
    });
    return S;
  }

  // Live fires: the published copy, shown at once. Meanwhile, if that copy is more than an hour behind
  // NASA (e.g. GitHub Actions is down), this browser reads NASA's own file with live.js (same rules)
  // and switches to it, telling the app through onLiveUpdate so it can redraw.
  const BEHIND_MS = 60 * 60 * 1000, liveListeners = [];
  let direct = null;
  async function upgradeFromNasa(lm) {
    if (!window.FireLive || navigator.connection?.saveData) return null;
    const head = await FireLive.nasaHead(), nasa = head?.modified;
    if (!nasa) return null;
    // same file as the published copy (a server re-stamping unchanged data): nothing new
    if (lm && head.size && lm.source_bytes && head.size === lm.source_bytes) return null;
    if (lm && nasa - new Date(lm.source_last_modified) <= BEHIND_MS) return null;
    const [mask, own, ix] = await Promise.all([ // without the gas-flare mask or the own cells, keep the published copy
      fetchJSON("data/live/static_cells.json"), fetchJSON("data/own_cells.json"),
      fetchJSON("data/live/tiles/index.json", { cache: "no-cache" }).catch(() => ({ tile_cells: 100 }))]);
    return { ...FireLive.build(await FireLive.fetchParsed(mask.cells), ix.tile_cells, nasa.toISOString()), own, direct: true };
  }
  const liveSource = () => direct ? Promise.resolve(direct) : once("livesrc", async () => {
    const lm = await fetchJSON("data/live/meta.json", { cache: "no-cache" }, 2).catch(() => null);
    const up = once("liveup", () => upgradeFromNasa(lm)).then((d) => {
      if (d) { direct = d; for (const f of liveListeners) try { f(d.meta); } catch (e) { console.error(e); } }
      return d;
    }).catch((e) => { console.warn("FireCal: reading live fires from NASA failed; showing the published copy", e); return null; });
    if (lm) return { meta: lm, direct: false };
    const d = await up; // no published copy at all: wait for NASA
    if (!d) throw new Error("live data unavailable");
    return d;
  });
  const liveOverview = async () => { const s = await liveSource(); return s.direct ? s.overview : once("liveov", () => fetchJSON("data/live/overview.json", { cache: "no-cache" })); };
  const liveIndex = async () => { const s = await liveSource(); return s.direct ? s.index : once("liveindex", () => fetchJSON("data/live/tiles/index.json", { cache: "no-cache" })); };
  const liveTile = async (k) => { const s = await liveSource(); return s.direct ? s.tiles[k] : once(`live:${k}`, () => fetchJSON(`data/live/tiles/${k}.json`, { cache: "no-cache" })); };
  async function liveCountry(id, days) {
    const s = await liveSource();
    if (s.direct) return once(`livec:${id}`, async () => {
      const feature = (await shapes()).features.find((f) => f.properties.id === id);
      return FireLive.countryCells(s, feature, s.own[id]);
    });
    const lc = await once("livecountries", () => fetchJSON("data/live/countries.json", { cache: "no-cache" }));
    return lc.countries[id]?.cells || days.map(() => 0);
  }

  const Static = {
    mode: "static",
    maxBoxDeg2: Infinity, // set from data/meta.json
    async meta() {
      META = await fetchJSON("data/meta.json", { cache: "no-cache" });
      this.maxBoxDeg2 = META.max_box_deg2;
      const live = await fetchJSON("data/live/meta.json", { cache: "no-cache" }, 2).catch(() => null);
      return { ...META, live };
    },
    async calendar(a, onProgress) {
      const c = a.country && META.countries.find((x) => x.id === a.country);
      if (a.country) {
        if (!c?.ready) return { unavailable: true, detail: `No harmonized record is available for ${c?.name || a.country} yet.` };
        return once(`cal:${a.country}`, () => fetchJSON(`data/countries/${encodeURIComponent(a.country)}.json`));
      }
      return once(`box:${a.bbox.join(",")}`, async () => {
        const [w, s, e, n] = a.bbox;
        const touched = await countriesTouching(a.bbox);
        const byId = Object.fromEntries(META.countries.map((x) => [x.id, x]));
        const ready = touched.filter((id) => byId[id]?.ready);
        if (!touched.length) return { unavailable: true, detail: "No land with fire records in this box. Try drawing over land." };
        if (!ready.length) return { unavailable: true, detail: `No harmonized record is published yet for ${touched.map((id) => byId[id]?.name || id).join(", ")}.` };
        const S = await boxSeries(a.bbox, onProgress);
        const label = `${Math.abs(s).toFixed(1)}°${s < 0 ? "S" : "N"}–${Math.abs(n).toFixed(1)}°${n < 0 ? "S" : "N"}, ${Math.abs(w).toFixed(1)}°${w < 0 ? "W" : "E"}–${Math.abs(e).toFixed(1)}°${e < 0 ? "W" : "E"}`;
        return FireEngine.analyze(S, META.prior, {
          aoi: a, label, view: a.bbox,
          countries: touched.filter((id) => byId[id]?.ready).map((id) => ({ id, name: byId[id].name })),
          missing: touched.filter((id) => byId[id] && !byId[id].ready).map((id) => ({ id, name: byId[id].name })),
        });
      });
    },
    async nowcast(a, cal) {
      const src = await liveSource(), lm = src.meta, days = lm.complete_days;
      let cells;
      if (a.country) cells = await liveCountry(a.country, days);
      else {
        const { y0, y1, x0, x1 } = cellBounds(a.bbox);
        const ix = await liveIndex(), want = [], T = ix.tile_cells;
        for (let ty = Math.floor(y0 / T); ty <= Math.floor(y1 / T); ty++)
          for (let tx = Math.floor(x0 / T); tx <= Math.floor(x1 / T); tx++)
            if (ix.tiles[`${ty}_${tx}`] != null) want.push(`${ty}_${tx}`);
        const counts = new Map(days.map((d) => [d, 0]));
        for (const t of await Promise.all(want.map(liveTile))) {
          for (const [di, xi, yi] of t.rows) {
            const d = t.days[di];
            if (counts.has(d) && yi >= y0 && yi <= y1 && xi >= x0 && xi <= x1) counts.set(d, counts.get(d) + 1);
          }
        }
        cells = days.map((d) => counts.get(d));
      }
      const h = cal?.daily?.h || null;
      return { ...FireEngine.nowcast(days, cells, h, +META.range.end.slice(0, 4)), fetched_at: lm.fetched_at,
               source_updated: lm.source_last_modified, direct_from_nasa: src.direct };
    },
    async grid(bbox, year, month, zoom) {
      const key = year ? `y${year}` : month ? `m${String(month).padStart(2, "0")}` : "all";
      if (zoom < 4) return once(`map:${key}:ov`, () => fetchJSON(`data/map/${key}/overview.json`)).catch(() => ({ cell: 0.5, max: 0, cells: [] }));
      const ix = await once(`map:${key}:ix`, () => fetchJSON(`data/map/${key}/index.json`)).catch(() => ({ tiles: [], max: 0 }));
      const have = new Set(ix.tiles), { y0, y1, x0, x1 } = cellBounds(bbox), want = [], T = ix.tile_cells || 100;
      for (let ty = Math.floor(y0 / T); ty <= Math.floor(y1 / T); ty++)
        for (let tx = Math.floor(x0 / T); tx <= Math.floor(x1 / T); tx++) if (have.has(`${ty}_${tx}`)) want.push(`${ty}_${tx}`);
      const parts = await Promise.all(want.map((k) => once(`map:${key}:${k}`, () => fetchJSON(`data/map/${key}/${k}.json`))));
      return { cell: 0.1, max: ix.max, cells: parts.flatMap((p) => p.cells) };
    },
    async live(bbox, zoom) {
      const lm = (await liveSource()).meta;
      if (zoom < 4) return { ...(await liveOverview()), fetched_at: lm.fetched_at };
      const ix = await liveIndex();
      const { y0, y1, x0, x1 } = cellBounds(bbox), want = [], T = ix.tile_cells;
      for (let ty = Math.floor(y0 / T); ty <= Math.floor(y1 / T); ty++)
        for (let tx = Math.floor(x0 / T); tx <= Math.floor(x1 / T); tx++) if (ix.tiles[`${ty}_${tx}`] != null) want.push(`${ty}_${tx}`);
      const sums = new Map();
      for (const t of await Promise.all(want.map(liveTile)))
        for (const [, xi, yi, n] of t.rows) { const k = `${xi},${yi}`; sums.set(k, (sums.get(k) || 0) + n); }
      const cells = [...sums].map(([k, v]) => { const [xi, yi] = k.split(",").map(Number); return [xi, yi, v]; });
      const vals = cells.map((c) => c[2]).sort((a, b) => a - b);
      return { cell: 0.1, max: vals.length ? vals[Math.floor(0.99 * (vals.length - 1))] : 0, cells, days: lm.days, fetched_at: lm.fetched_at };
    },
    async locate(lon, lat) {
      const f = (await shapes()).features.find((f) => FireGeo.pointTouches(f, lon, lat));
      const id = f?.properties.id || null;
      return { country: id, name: id ? META.countries.find((c) => c.id === id)?.name : null };
    },
    prepare: async () => ({}),
    jobs: async () => ({}),
    onLiveUpdate(f) { liveListeners.push(f); }, // called with the new live meta when NASA's own data replaces the published copy
  };

  window.FireData = STATIC ? Static : Server;
  window.FireData.HttpError = HttpError;
})();
