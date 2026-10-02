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
  // this site's files fetched so far, so the offline helper can save the ones loaded before it started
  const fetched = new Set();
  async function fetchJSON(url, opts = {}, tries = 3) {
    if (!/^https?:/.test(url) && !(opts.method && opts.method !== "GET")) fetched.add(url);
    for (let i = 0; ; i++) {
      try {
        const r = await fetch(url, opts);
        const body = await r.json().catch(() => null);
        if (!r.ok) throw new HttpError(r.status, body);
        return body;
      } catch (e) {
        if (!navigator.onLine && !(e instanceof HttpError))  // airplane mode, and this file isn't saved on the device
          throw new Error("you're offline and this hasn't been saved on this device yet. Reconnect, or next time use ⤓ → Save all countries before going offline");
        if (i + 1 >= tries || (e instanceof HttpError && e.status < 500)) throw e;
        await new Promise((res) => setTimeout(res, 600 * 2 ** i));
      }
    }
  }
  // helpers loaded only by older browsers that lack a feature (integrity-checked)
  const loaded = new Map();
  function loadScript(url, integrity) {
    if (!loaded.has(url)) loaded.set(url, new Promise((resolve, reject) => {
      const s = document.createElement("script");
      Object.assign(s, { src: url, integrity, crossOrigin: "anonymous", onload: resolve, onerror: () => { loaded.delete(url); reject(new Error("could not load a helper; check the connection")); } });
      document.head.appendChild(s);
    }));
    return loaded.get(url);
  }
  function loadStyle(url, integrity) {
    if (document.querySelector(`link[href="${url}"]`)) return;
    const l = document.createElement("link");
    Object.assign(l, { rel: "stylesheet", href: url, integrity, crossOrigin: "anonymous" });
    document.head.appendChild(l);
  }
  window.FireLoad = { script: loadScript, style: loadStyle };
  const PAKO = ["https://cdnjs.cloudflare.com/ajax/libs/pako/2.1.0/pako_inflate.min.js", "sha384-taEjHL+GUvC8IhXeCaTNUxz3O8ItsajGFLDFj4v/VJvM24HU36qP/6sRpuAGGfeT"];

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
    if (!("DecompressionStream" in window)) { // older browsers (e.g. iOS before 16.4): a small JavaScript inflater
      await loadScript(...PAKO);
      const out = window.pako.ungzip(buf);
      return out.buffer.slice(out.byteOffset, out.byteOffset + out.byteLength);
    }
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

  // Live fires. The published copy shows at once; independently of GitHub, the browser asks NASA (a few
  // hundred bytes) on opening and every 10 minutes whether it has newer fires. If so, it reads NASA's
  // last-24-hours file, joins it to the earlier days it already has, and switches to that (live.js, same
  // rules as the cloud); when GitHub's published copy catches up, it switches back. App: onLiveUpdate.
  const RECHECK_MS = 10 * 60 * 1000, GITHUB_GRACE_MS = 15 * 60 * 1000, liveListeners = [];
  let published = null, direct = null, checking = null, timer = null;
  // is live data `a` newer than `b`? (published meta: latest_detection; NASA file parsed by live.js: latest)
  const latestOf = (x) => x?.latest_detection ?? x?.latest ?? null;
  const newer = (a, b) => !!latestOf(a) && (!latestOf(b) || latestOf(a) > latestOf(b));
  function resetLive() { for (const k of [...cache.keys()]) if (k.startsWith("live")) cache.delete(k); }
  function announce(meta) { for (const f of liveListeners) try { f(meta); } catch (e) { console.error(e); } }

  async function allTiles(src) { // every live tile of what is shown now (published: ~0.5 MB, fetched once)
    if (src.direct) return src.tiles;
    const ix = await liveIndex(), keys = Object.keys(ix.tiles), out = {};
    await pool(keys, 6, async (k) => { out[k] = await liveTile(k); });
    return out;
  }

  async function fromNasa(cur) { // newer live data from NASA than `cur`, or null
    if (!window.FireLive || navigator.connection?.saveData) return null;
    const head = await FireLive.nasaHead();
    if (!head) return null;
    if (cur.meta.source_bytes && head.size === cur.meta.source_bytes) return null; // the same file
    if (head.modified <= new Date(cur.meta.source_last_modified)) return null;    // the mirror isn't ahead
    if (Date.now() - head.modified < GITHUB_GRACE_MS && !cur.direct) return null;  // just updated: GitHub is on it
    const [mask, own, ix] = await Promise.all([ // without the gas-flare mask or the own cells, keep the published copy
      fetchJSON("data/live/static_cells.json"), fetchJSON("data/own_cells.json"),
      fetchJSON("data/live/tiles/index.json", { cache: "no-cache" }).catch(() => ({ tile_cells: 100 }))]);
    const fresh = await FireLive.fetchParsed(mask.cells, FireLive.FEED24);
    if (!newer(fresh, cur.meta)) return null; // e.g. the mirror is behind the server GitHub read
    let cd = FireLive.merge(FireLive.fromTiles(await allTiles(cur)), fresh);
    if (!cd) { // the copy shown is more than a day old: read NASA's whole 7-day file
      cd = await FireLive.fetchParsed(mask.cells, FireLive.FEED);
      if (!newer(cd, cur.meta)) return null;
    }
    const live = FireLive.build(cd, ix.tile_cells, head.modified.toISOString());
    live.meta.source_bytes = head.size;
    return { ...live, own, direct: true };
  }

  function checkNasa() {
    if (!checking) checking = (async () => {
      const cur = direct || { meta: published, direct: false };
      if (!cur.meta) return null;
      const d = await fromNasa(cur);
      if (d) { direct = d; resetLive(); announce(d.meta); }
      return d;
    })().catch((e) => { console.warn("FireCal: couldn't read live fires from NASA; keeping what is shown", e); return null; })
      .finally(() => { checking = null; });
    return checking;
  }

  async function recheck() { // every 10 minutes while the page is open
    const lm = await fetchJSON("data/live/meta.json", { cache: "no-cache" }, 2).catch(() => null);
    if (lm && newer(lm, direct ? direct.meta : published)) { // GitHub has caught up (or moved ahead)
      published = lm; direct = null; resetLive();
      cache.set("livesrc", Promise.resolve({ meta: lm, direct: false }));
      announce(lm);
    }
    await checkNasa();
  }

  const liveSource = () => direct ? Promise.resolve(direct) : once("livesrc", async () => {
    published = await fetchJSON("data/live/meta.json", { cache: "no-cache" }, 2).catch(() => null);
    if (!timer) timer = setInterval(() => { if (document.visibilityState !== "hidden") recheck(); }, RECHECK_MS);
    if (published) { checkNasa(); return { meta: published, direct: false }; }
    // nothing published yet: read NASA's 7-day file directly
    const mask = await fetchJSON("data/live/static_cells.json"), own = await fetchJSON("data/own_cells.json");
    const head = await FireLive.nasaHead(), cd = await FireLive.fetchParsed(mask.cells, FireLive.FEED);
    direct = { ...FireLive.build(cd, 100, (head?.modified || new Date()).toISOString()), own, direct: true };
    return direct;
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

  const MAX_DETAIL_TILES = 12, MAX_MAP_CELLS = 20000; // as Store._coarsen on the server
  const LIVE_MAX_TILES = 60; // live tiles hold only this week's fires (a few KB each), so many fit
  // merge f×f blocks until at most `limit` squares: "mean" per 0.1° cell (history) or "sum" (live detections)
  function coarsen(cells, limit = MAX_MAP_CELLS, how = "mean") {
    let f = 1, out = cells;
    while (out.length > limit) {
      f += 1;
      const sums = new Map();
      for (const [xi, yi, v] of cells) { const k = `${Math.floor(xi / f)},${Math.floor(yi / f)}`; sums.set(k, (sums.get(k) || 0) + v); }
      const div = how === "mean" ? f * f : 1;
      out = [...sums].map(([k, v]) => { const [a, b] = k.split(",").map(Number); return [a, b, Math.round((v / div) * 100) / 100]; });
    }
    return { cell: f / CELL, cells: out };
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
    async grid(bbox, year, month) {
      // like the server: full 0.1° detail whenever the view needs at most MAX_DETAIL_TILES tiles (country
      // views), merged into coarser squares only as far as needed to stay under MAX_MAP_CELLS; the 0.5°
      // world overview otherwise
      const key = year ? `y${year}` : month ? `m${String(month).padStart(2, "0")}` : "all";
      const overview = () => once(`map:${key}:ov`, () => fetchJSON(`data/map/${key}/overview.json`));
      const ix = await once(`map:${key}:ix`, () => fetchJSON(`data/map/${key}/index.json`)).catch(() => null);
      if (!ix) return overview();
      const have = new Set(ix.tiles), { y0, y1, x0, x1 } = cellBounds(bbox), want = [], T = ix.tile_cells || 100;
      if ((Math.floor(y1 / T) - Math.floor(y0 / T) + 1) * (Math.floor(x1 / T) - Math.floor(x0 / T) + 1) > 4 * MAX_DETAIL_TILES) return overview();
      for (let ty = Math.floor(y0 / T); ty <= Math.floor(y1 / T); ty++)
        for (let tx = Math.floor(x0 / T); tx <= Math.floor(x1 / T); tx++) if (have.has(`${ty}_${tx}`)) want.push(`${ty}_${tx}`);
      if (want.length > MAX_DETAIL_TILES) return overview();
      let parts;
      try { parts = await Promise.all(want.map((k) => once(`map:${key}:${k}`, () => fetchJSON(`data/map/${key}/${k}.json`)))); }
      catch (e) { if (!navigator.onLine) return overview(); throw e; } // offline: the saved 0.5° overview instead of failing
      const cells = parts.flatMap((p) => p.cells).filter(([xi, yi]) => xi >= x0 && xi <= x1 && yi >= y0 && yi <= y1);
      return { max: ix.max, ...coarsen(cells) };
    },
    async live(bbox) {
      // as the server (Store.live): detections per 0.1° cell in view, merged only beyond 15,000 squares,
      // coloured up to the 99th percentile; the world overview when the view needs too many tiles
      const lm = (await liveSource()).meta;
      const ix = await liveIndex();
      const { y0, y1, x0, x1 } = cellBounds(bbox), want = [], T = ix.tile_cells;
      const span = (Math.floor(y1 / T) - Math.floor(y0 / T) + 1) * (Math.floor(x1 / T) - Math.floor(x0 / T) + 1);
      if (span <= 4 * LIVE_MAX_TILES)
        for (let ty = Math.floor(y0 / T); ty <= Math.floor(y1 / T); ty++)
          for (let tx = Math.floor(x0 / T); tx <= Math.floor(x1 / T); tx++) if (ix.tiles[`${ty}_${tx}`] != null) want.push(`${ty}_${tx}`);
      if (span > 4 * LIVE_MAX_TILES || want.length > LIVE_MAX_TILES) return { ...(await liveOverview()), fetched_at: lm.fetched_at };
      let tiles;
      try { tiles = await Promise.all(want.map(liveTile)); }
      catch (e) { if (!navigator.onLine) return { ...(await liveOverview()), fetched_at: lm.fetched_at }; throw e; } // offline: saved overview
      const sums = new Map();
      for (const t of tiles)
        for (const [, xi, yi, n] of t.rows)
          if (xi >= x0 && xi <= x1 && yi >= y0 && yi <= y1) { const k = `${xi},${yi}`; sums.set(k, (sums.get(k) || 0) + n); }
      const c = coarsen([...sums].map(([k, v]) => { const [xi, yi] = k.split(",").map(Number); return [xi, yi, v]; }), 15000, "sum");
      const vals = c.cells.map((x) => x[2]).sort((a, b) => a - b);
      return { ...c, max: Math.round(FireLive.quantile(vals, 0.99) * 100) / 100, days: lm.days, fetched_at: lm.fetched_at };
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

  Static.fetchedUrls = Server.fetchedUrls = () => [...fetched];
  window.FireData = STATIC ? Static : Server;
  window.FireData.HttpError = HttpError;
})();
