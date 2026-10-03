/* FireCal service worker: an installable app that also works offline (airplane mode).
   Works at a site root (local server) or under a sub-path such as /firecal/ (GitHub Pages).

   On install it saves the whole app shell (code, styles, icons, fonts, map library, country borders,
   the world fire map and the world base map down to zoom 3), so after one visit FireCal opens with no
   connection at all. Areas you view are saved as you go; "Save all countries for offline" in the app
   saves every country's calendar and every map layer (see app.js). */
const VERSION = "{{v}}";
const EDITION = "{{edition}}"; // "static" (the website) or "server" (this computer); set where this file is served
const SHELL = `firecal-shell-${VERSION}`;
const DATA = "firecal-data-v2";
const LIBS = "firecal-libs-v1";
const TILES = "firecal-tiles-v1";
const FONTS = "firecal-fonts-v1";
const BASE = new URL(self.registration.scope).pathname; // e.g. "/" or "/firecal/"

const STATIC_FILES = ["styles.css", "app.js", "data.js", "engine.js", "geo.js", "live.js", "vendor/echarts.min.js"];
const ESSENTIAL = ["./", ...STATIC_FILES.map((f) => `static/${f}?v=${VERSION}`)]; // the page and its code
const SHELL_URLS = [...ESSENTIAL, "manifest.webmanifest", "world.geojson", ...(EDITION === "static" ? ["shapes.geojson"] : []),
  // the page asks for these three with ?v= (index.html); the manifest's icons without
  ...["icon.svg", "apple-touch-icon.png", "favicon-32.png"].map((f) => `static/${f}?v=${VERSION}`),
  "static/icon-192.png", "static/icon-512.png", "static/icon-maskable-512.png", "static/apple-touch-icon.png", "static/icon.svg"];
// the website's published files (the local server answers /api/ instead)
const DATA_URLS = EDITION !== "static" ? [] : ["data/meta.json", "data/map/all/overview.json", "data/live/meta.json", "data/live/overview.json",
                   "data/live/tiles/index.json", "data/own_cells.json", "data/live/static_cells.json"];
const LIB_URLS = ["https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.js",
                  "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.css"];
const FONT_CSS = "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap";

const pause = (ms) => new Promise((r) => setTimeout(r, ms));

function worldTiles() { // the base map of the whole world down to zoom 3 (85 small tiles per theme)
  const out = [];
  for (const theme of ["Light", "Dark"])
    for (let z = 0; z <= 3; z++)
      for (let y = 0; y < 2 ** z; y++)
        for (let x = 0; x < 2 ** z; x++)
          out.push(`https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_${theme}_Gray_Base/MapServer/tile/${z}/${y}/${x}`);
  return out;
}

async function saveAll(cacheName, urls, opts) { // best effort: one missing file never blocks the rest
  const cache = await caches.open(cacheName);
  await Promise.allSettled(urls.map(async (u) => {
    if (opts?.skipCached && await cache.match(u)) return;
    const res = await fetch(u, opts?.cors ? { mode: "cors" } : {});
    if (res.ok || res.type === "opaque") await cache.put(u, res);
  }));
}

async function saveFonts() { // the font stylesheet and the font files it points to
  try {
    const res = await fetch(FONT_CSS);
    if (!res.ok) return;
    const css = await res.clone().text();
    await (await caches.open(FONTS)).put(FONT_CSS, res);
    await saveAll(FONTS, [...css.matchAll(/url\((https:[^)]+)\)/g)].map((m) => m[1]));
  } catch (_) { /* offline fonts fall back to the system font */ }
}

// The page and its code, retried over a shaky connection. A new version always installs (an old worker must never
// stay in charge just because one download failed), but the previous saved copy is removed only once this one is
// complete, so there is always a full copy to open offline.
async function saveEssential() {
  const shell = await caches.open(SHELL);
  for (const wait of [0, 1000, 3000]) {
    if (wait) await pause(wait);
    await Promise.allSettled(ESSENTIAL.map(async (u) => {
      if (await shell.match(u)) return;
      const res = await fetch(u, { cache: "no-cache" });
      if (res.ok) await shell.put(u, res);
    }));
    if (await shellComplete()) return;
  }
}
async function shellComplete() {
  const shell = await caches.open(SHELL);
  return (await Promise.all(ESSENTIAL.map((u) => shell.match(u)))).every(Boolean);
}
// Saved copies, newest first: this version's, then any earlier one still kept (see above). Other apps on the same
// web address share this storage and may have deleted ours, so a miss is normal and handled by the callers.
async function fromShells(req, opts) {
  const older = (await caches.keys()).filter((k) => k.startsWith("firecal-shell-") && k !== SHELL);
  for (const name of [SHELL, ...older]) {
    const hit = await (await caches.open(name)).match(req, opts);
    if (hit) return hit;
  }
}

self.addEventListener("install", (e) => {
  self.skipWaiting(); // a new version always takes over: installing never waits on, or fails because of, saving
  e.waitUntil((async () => {
    try {
      await saveAll(SHELL, SHELL_URLS);                    // the app itself
      await saveEssential();
      await Promise.allSettled([saveAll(DATA, DATA_URLS), saveAll(LIBS, LIB_URLS), saveFonts()]);
    } catch (_) { /* storage refused or unavailable (e.g. a full disk): the app still works online */ }
    // in the background, and not on Data Saver or 2G (those tiles are still saved as the map shows them)
    const net = self.navigator.connection;
    if (!(net?.saveData || /2g/.test(net?.effectiveType || ""))) saveAll(TILES, worldTiles(), { skipCached: true });
  })());
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    if (await shellComplete()) {
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k.startsWith("firecal-shell-") && k !== SHELL).map((k) => caches.delete(k)));
    }
    // earlier versions turned navigation preload on; pages now open without this worker while online
    try { await self.registration.navigationPreload?.disable(); } catch (_) { /* not supported: no change */ }
    await self.clients.claim();
  })());
});

// the page asks for everything to be saved for offline use; progress is reported back to it
self.addEventListener("message", (e) => {
  if (e.data?.type !== "save-offline") return;
  const urls = e.data.urls, port = e.ports[0];
  e.waitUntil((async () => {
    const cache = await caches.open(DATA);
    let done = 0, failed = 0, i = 0;
    await Promise.all(Array.from({ length: 6 }, async () => {
      while (i < urls.length) {
        const u = urls[i++];
        try {
          const res = await fetch(u, { cache: "no-cache" });
          if (res.ok) await cache.put(u, res); else failed++;
        } catch (_) { failed++; }
        done++;
        if (done % 10 === 0 || done === urls.length) port?.postMessage({ done, total: urls.length, failed });
      }
    }));
    port?.postMessage({ done, total: urls.length, failed, finished: true });
  })());
});

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(req) || await cache.match(req, { ignoreSearch: true });
    if (hit) return hit;
    throw err;
  }
}

async function appFile(req) { // versioned code and styles: from any saved copy, else the network (and saved)
  const hit = await fromShells(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(req, copy)).catch(() => {}); }
  return res;
}

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok || res.type === "opaque") cache.put(req, res.clone()).then(() => cacheName === TILES && trimTiles()).catch(() => {});
  return res;
}

// base-map tiles are saved as they are viewed; keep the most recent MAX_TILES so storage can't grow forever
const MAX_TILES = 4000; // ≈ 60–80 MB of tiles: the world overview plus a few countries in detail
let trimming = null, added = 0;
function trimTiles() {
  if (++added % 100 || trimming) return; // check every 100 new tiles
  trimming = (async () => {
    const cache = await caches.open(TILES);
    // oldest first; the world overview saved at install (zoom 0–3, see worldTiles) is always kept
    const keys = (await cache.keys()).filter((k) => +(/\/tile\/(\d+)\//.exec(k.url)?.[1] ?? 99) > 3);
    await Promise.all(keys.slice(0, Math.max(0, keys.length - MAX_TILES)).map((k) => cache.delete(k)));
  })().catch(() => {}).finally(() => { trimming = null; });
}

async function staleWhileRevalidate(req, cacheName, event) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req) || await cache.match(req, { ignoreSearch: true });
  const update = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; });
  if (hit) { event.waitUntil(update.catch(() => {})); return hit; }
  return update;
}

async function pageFromNetwork(e) {
  try { return await fetch(e.request); }
  catch (_) { await pause(800); return fetch(e.request); } // a brief blip (Wi-Fi hiccup, network change, waking up)
}

// Shown only when the network is down and nothing was saved yet: it says what is going on and reloads by itself
// once FireCal answers again, instead of the browser's "This site can't be reached" (ERR_FAILED) page.
const RECONNECT = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>FireCal · reconnecting</title>
<style>:root{color-scheme:light dark;--bg:#fff8f3;--fg:#2a1a12;--mut:#7a5a48}
@media (prefers-color-scheme:dark){:root{--bg:#1b1210;--fg:#fbeee6;--mut:#c9a898}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;text-align:center;padding:16px;box-sizing:border-box}
main{max-width:26rem}.logo{font-size:2.6rem}h1{margin:.3rem 0;font-size:1.5rem}
h1 b{background:linear-gradient(90deg,#ffb020,#ff5a1f);-webkit-background-clip:text;background-clip:text;color:transparent}
p{color:var(--mut);margin:.4rem 0 1.2rem}
button{font:inherit;font-weight:600;color:#fff;border:0;border-radius:12px;padding:.75rem 1.4rem;min-height:44px;cursor:pointer;
background:linear-gradient(90deg,#ff8a1f,#e5381a)}</style></head><body><main>
<div class="logo" aria-hidden="true">🔥</div><h1><b>FireCal</b> is reconnecting…</h1>
<p>Your device lost its internet connection for a moment. This page reloads by itself as soon as it is back.</p>
<button onclick="location.reload()">Try again now</button></main>
<script>addEventListener("online",()=>location.reload());
setInterval(()=>fetch(location.href,{cache:"no-store",method:"HEAD"}).then(r=>{if(r.ok)location.reload()},()=>{}),5000);</script>
</body></html>`;

async function page(e) { // the app page: fresh when online, the saved copy offline (any #hash or ?query)
  try {
    const res = await pageFromNetwork(e);
    if (res.ok) { const copy = res.clone(); e.waitUntil(caches.open(SHELL).then((c) => c.put("./", copy)).catch(() => {})); }
    return res;
  } catch (_) {
    try {
      const hit = (await fromShells("./")) || (await fromShells(e.request, { ignoreSearch: true }));
      if (hit) return hit;
    } catch (_) { /* storage unavailable: show the reconnecting page */ }
    return new Response(RECONNECT, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
  }
}

// if anything here fails (storage full or unavailable), the file comes straight from the network, as without this worker
const answer = (e, work) => e.respondWith(work.catch(() => fetch(e.request)));

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (req.mode === "navigate" && url.origin === location.origin) {
    // Online, the browser opens the page itself, exactly as if this worker did not exist, so nothing here
    // (storage, a stuck request, another app's cleanup) can ever stand between a visitor and FireCal.
    // Offline (airplane mode, no network) it opens the saved copy.
    if (self.navigator.onLine !== false) return;
    e.respondWith(page(e)); return;
  }
  if (url.origin === location.origin) {
    const p = url.pathname.slice(BASE.length - 1); // path relative to the app root, starting with "/"
    if (p.startsWith("/api/jobs") || p.startsWith("/api/prepare")) return;                                  // progress: always live
    if (p.startsWith("/api/") || p.startsWith("/data/live/") || p.startsWith("/data/meta.json"))
      answer(e, networkFirst(req, DATA));                                                                   // fresh, offline fallback
    else if (p.startsWith("/data/") || p.startsWith("/places/")) answer(e, staleWhileRevalidate(req, DATA, e)); // history, place names
    else if (p.startsWith("/static/")) answer(e, appFile(req));                                           // versioned URLs
    else answer(e, networkFirst(req, SHELL));
  } else if (url.hostname === "cdnjs.cloudflare.com") {
    answer(e, cacheFirst(req, LIBS));
  } else if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") {
    answer(e, cacheFirst(req, FONTS));
  } else if (url.hostname === "server.arcgisonline.com") {
    answer(e, cacheFirst(req, TILES));
  }
});
