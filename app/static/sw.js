/* FireCal service worker: installable app shell, offline fallback for data already viewed. */
const VERSION = "{{v}}";
const SHELL = `firecal-shell-${VERSION}`;
const DATA = "firecal-data-v1";
const LIBS = "firecal-libs-v1";

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(["/", "/world.geojson", "/manifest.webmanifest"])).then(() => self.skipWaiting()));
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
    const hit = await cache.match(req);
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

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin === location.origin) {
    if (url.pathname.startsWith("/api/jobs") || url.pathname.startsWith("/api/meta")) return; // always live
    if (url.pathname.startsWith("/api/")) e.respondWith(networkFirst(req, DATA));        // offline: last seen
    else if (url.pathname.startsWith("/static/")) e.respondWith(cacheFirst(req, SHELL));   // versioned URLs
    else e.respondWith(networkFirst(req, SHELL));
  } else if (url.hostname === "cdnjs.cloudflare.com") {
    e.respondWith(cacheFirst(req, LIBS));
  } else if (url.hostname === "server.arcgisonline.com") {
    e.respondWith(cacheFirst(req, "firecal-tiles-v1"));
  }
});
