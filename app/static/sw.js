/* FireCal service worker: an installable app that also works offline (airplane mode).
   Works at a site root (local server) or under a sub-path such as /firecal/ (GitHub Pages).

   On install it saves the whole app shell (code, styles, icons, fonts, map library, country borders,
   the world fire map and the world base map down to zoom 3), so after one visit FireCal opens with no
   connection at all. Areas you view are saved as you go; "Save all countries for offline" in the app
   saves every country's calendar and every map layer (see app.js). */
const VERSION = "{{v}}";
const SHELL = `firecal-shell-${VERSION}`;
const DATA = "firecal-data-v2";
const LIBS = "firecal-libs-v1";
const TILES = "firecal-tiles-v1";
const FONTS = "firecal-fonts-v1";
const BASE = new URL(self.registration.scope).pathname; // e.g. "/" or "/firecal/"

const STATIC_FILES = ["styles.css", "app.js", "data.js", "engine.js", "geo.js", "live.js", "vendor/echarts.min.js"];
const SHELL_URLS = ["./", "manifest.webmanifest", "world.geojson", "shapes.geojson",
  ...STATIC_FILES.map((f) => `static/${f}?v=${VERSION}`),
  "static/icon.svg", "static/icon-192.png", "static/icon-512.png", "static/icon-maskable-512.png", "static/apple-touch-icon.png", "static/favicon-32.png"];
const DATA_URLS = ["data/meta.json", "data/map/all/overview.json", "data/live/meta.json", "data/live/overview.json",
                   "data/live/tiles/index.json", "data/own_cells.json", "data/live/static_cells.json"];
const LIB_URLS = ["https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.js",
                  "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.css"];
const FONT_CSS = "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap";

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

self.addEventListener("install", (e) => {
  e.waitUntil((async () => {
    await saveAll(SHELL, SHELL_URLS);                      // the app itself
    await Promise.allSettled([saveAll(DATA, DATA_URLS), saveAll(LIBS, LIB_URLS), saveFonts()]);
    await self.skipWaiting();
    saveAll(TILES, worldTiles(), { skipCached: true });   // in the background: not needed to start
  })());
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith("firecal-shell-") && k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
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

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok || res.type === "opaque") cache.put(req, res.clone());
  return res;
}

async function staleWhileRevalidate(req, cacheName, event) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req) || await cache.match(req, { ignoreSearch: true });
  const update = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; });
  if (hit) { event.waitUntil(update.catch(() => {})); return hit; }
  return update;
}

async function page(req) { // the app page: fresh when online, the saved copy offline (any #hash or ?query)
  try {
    const res = await fetch(req);
    if (res.ok) (await caches.open(SHELL)).put("./", res.clone());
    return res;
  } catch (err) {
    const shell = await caches.open(SHELL);
    return (await shell.match("./")) || (await shell.match(req, { ignoreSearch: true })) || Response.error();
  }
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (req.mode === "navigate" && url.origin === location.origin) { e.respondWith(page(req)); return; }
  if (url.origin === location.origin) {
    const p = url.pathname.slice(BASE.length - 1); // path relative to the app root, starting with "/"
    if (p.startsWith("/api/jobs") || p.startsWith("/api/prepare")) return;                                  // progress: always live
    if (p.startsWith("/api/") || p.startsWith("/data/live/") || p.startsWith("/data/meta.json"))
      e.respondWith(networkFirst(req, DATA));                                                               // fresh, offline fallback
    else if (p.startsWith("/data/")) e.respondWith(staleWhileRevalidate(req, DATA, e));                    // precomputed history
    else if (p.startsWith("/static/")) e.respondWith(cacheFirst(req, SHELL));                              // versioned URLs
    else e.respondWith(networkFirst(req, SHELL));
  } else if (url.hostname === "cdnjs.cloudflare.com") {
    e.respondWith(cacheFirst(req, LIBS));
  } else if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") {
    e.respondWith(cacheFirst(req, FONTS));
  } else if (url.hostname === "server.arcgisonline.com") {
    e.respondWith(cacheFirst(req, TILES));
  }
});
