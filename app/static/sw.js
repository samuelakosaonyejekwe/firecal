/* FireCal service worker: installable app shell + offline access to what you've already viewed.
   Works at a site root (server edition) or under a sub-path such as /firecal/ (GitHub Pages). */
const VERSION = "{{v}}";
const SHELL = `firecal-shell-${VERSION}`;
const DATA = "firecal-data-v2";
const LIBS = "firecal-libs-v1";
const BASE = new URL(self.registration.scope).pathname; // e.g. "/" or "/firecal/"

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(["./", "world.geojson", "manifest.webmanifest"])).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith("firecal-shell-") && k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(req, { ignoreSearch: false });
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
  const hit = await cache.match(req);
  const update = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; });
  if (hit) { event.waitUntil(update.catch(() => {})); return hit; }
  return update;
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
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
  } else if (url.hostname === "server.arcgisonline.com") {
    e.respondWith(cacheFirst(req, "firecal-tiles-v1"));
  }
});
