/* FireCal front end — world map + harmonized burning calendar for any area of interest. */
"use strict";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const $ = (id) => document.getElementById(id);
const state = {
  meta: null, byId: {}, aoi: null, data: null, now: null,
  calMode: "value", season: null, dayYear: null, layer: "history", audience: "responders", req: 0,
};
const charts = {};

// ───────────────────────── utilities ─────────────────────────
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmt = (v, d = 0) => (v == null || !isFinite(v) ? "–" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d }));
const signed = (v, d = 1) => (v == null || !isFinite(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d));
const addDays = (iso, n) => { const t = new Date(iso + "T00:00:00Z"); t.setUTCDate(t.getUTCDate() + n); return t; };
const isoOf = (t) => t.toISOString().slice(0, 10);
const niceDate = (iso, year = true) => new Date(iso + "T00:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(year ? { year: "numeric" } : {}), timeZone: "UTC" });
const quantile = (arr, q) => { const a = arr.filter((v) => v != null && isFinite(v)).sort((x, y) => x - y); return a.length ? a[Math.min(a.length - 1, Math.floor(q * a.length))] : 0; };
const ramp = () => ["--q0", "--q1", "--q2", "--q3", "--q4", "--q5", "--q6"].map(css);
const diverging = () => ["--d-2", "--d-1", "--d0", "--d1", "--d2"].map(css);
const lat = (v) => `${Math.abs(v).toFixed(1)}°${v < 0 ? "S" : "N"}`;
const lon = (v) => `${Math.abs(v).toFixed(1)}°${v < 0 ? "W" : "E"}`;
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const ordinal = (n) => { n = Math.round(n); const s = ["th", "st", "nd", "rd"], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); };
const kfmt = (v) => (Math.abs(v) >= 1e6 ? `${v / 1e6}M` : Math.abs(v) >= 1000 ? `${v / 1000}k` : v);
const store = { get: (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } },
                set: (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} } };

function isDark() {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
}

function linfit(xs, ys) { // least-squares slope + mean
  const pts = xs.map((x, i) => [x, ys[i]]).filter(([, y]) => y != null && isFinite(y));
  if (pts.length < 5) return null;
  const mx = pts.reduce((a, p) => a + p[0], 0) / pts.length, my = pts.reduce((a, p) => a + p[1], 0) / pts.length;
  const sxx = pts.reduce((a, p) => a + (p[0] - mx) ** 2, 0);
  if (!sxx) return null;
  const slope = pts.reduce((a, p) => a + (p[0] - mx) * (p[1] - my), 0) / sxx;
  return { slope, mean: my };
}

function toast(msg, ms = 2600) { // plain text (never HTML)
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), ms);
}

// ───────────────────────── area of interest ─────────────────────────
const aoiQuery = (a) => (a.country ? `country=${encodeURIComponent(a.country)}` : `bbox=${a.bbox.join(",")}`);
const aoiKey = (a) => (a ? aoiQuery(a) : "");

// a country id or name from a link, in any letter case or spacing ("cyprus", "United States", "United_States")
function findCountry(q) {
  const k = String(q || "").trim().toLowerCase().replace(/[\s_]+/g, " ");
  if (!k) return null;
  return state.meta.countries.find((c) => c.id.toLowerCase().replace(/_/g, " ") === k || c.name.toLowerCase() === k) || null;
}

let hashProblem = ""; // why a link's area couldn't be used (shown once the page has loaded)
function aoiFromHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  hashProblem = "";
  if ((p.get("country") || "").trim()) {
    const c = findCountry(p.get("country"));
    if (c) return { country: c.id };
    hashProblem = `The link asked for “${p.get("country").slice(0, 60)}”, which isn't a country FireCal knows. Showing another area instead; search for the one you want above.`;
  }
  if (p.get("bbox")) {
    const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
    const r = p.get("bbox").split(",").map(Number);
    if (r.length === 4 && r.every(isFinite)) {
      const b = [clamp(r[0], -180, 180), clamp(r[1], -90, 90), clamp(r[2], -180, 180), clamp(r[3], -90, 90)];
      if (b[0] < b[2] && b[1] < b[3]) return { bbox: b };
    }
    hashProblem = "The link's box coordinates aren't valid (west, south, east, north in degrees). Showing another area instead.";
  }
  return null;
}

const aoiView = (a) => (a.bbox ? a.bbox : state.byId[a.country]?.view);

function selectAOI(a, { fly = true } = {}) {
  if (!a) return;
  if (aoiKey(a) === aoiKey(state.aoi) && state.data) return;
  state.aoi = a;
  history.replaceState(null, "", `#${aoiQuery(a)}`);
  store.set("firecal-aoi", aoiQuery(a));
  $("search").value = a.country ? state.byId[a.country].name : "";
  drawAOI();
  const v = aoiView(a);
  // (map.loaded() is false whenever any tile is still loading, which skipped this move; once the map's own
  // sources exist it can always move, and before that its load handler fits the selected area itself)
  if (fly && v && map?.getSource("aoi")) map.fitBounds([[v[0], v[1]], [v[2], v[3]]], { padding: 24, maxZoom: 6.5, duration: 700 });
  loadAOI();
  if (matchMedia("(max-width: 1100px)").matches) $("aoiHead").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ───────────────────────── map ─────────────────────────
let map = null, drawing = false, dragStart = null;
const ATTRIB = "Basemap © Esri · Boundaries © Natural Earth · Fire data: NASA FIRMS";
const tiles = () => [`https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_${isDark() ? "Dark" : "Light"}_Gray_Base/MapServer/tile/{z}/{y}/{x}`];
const rectFeature = ([w, s, e, n]) => ({ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] } });
const empty = { type: "FeatureCollection", features: [] };

function initMap() {
  try {
    map = new maplibregl.Map({
      container: "map",
      style: { version: 8, sources: { base: { type: "raster", tiles: tiles(), tileSize: 256, attribution: ATTRIB } },
               layers: [{ id: "base", type: "raster", source: "base" }] },
      center: [15, 12], zoom: 1.2, minZoom: 0.5, maxZoom: 11, attributionControl: { compact: true },
      renderWorldCopies: false, dragRotate: false, pitchWithRotate: false, touchPitch: false, cooperativeGestures: false,
      boxZoom: false, // Shift-click belongs to "analyze just this area"; Draw area is FireCal's own box tool
    });
  } catch (err) {
    mapUnavailable(); return;
  }
  map.on("error", (e) => { if (/webgl/i.test(e?.error?.message || "")) mapUnavailable(); });
  // keep the credits as a small (i) button the visitor can open, instead of a box over the map
  map.once("load", () => document.querySelector("#map .maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show"));
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.touchZoomRotate.disableRotation();

  map.on("load", () => {
    map.addSource("countries", { type: "geojson", data: "world.geojson", promoteId: "id" });
    map.addLayer({ id: "countries-fill", type: "fill", source: "countries", paint: { "fill-color": css("--ink"), "fill-opacity": 0.001 } });
    map.addLayer({ id: "countries-hover", type: "fill", source: "countries", filter: ["==", ["get", "id"], ""], paint: { "fill-color": css("--accent"), "fill-opacity": 0.12 } });
    map.addSource("cells", { type: "geojson", data: empty });
    map.addLayer({ id: "cells", type: "fill", source: "cells", paint: { "fill-opacity": 0.85, "fill-color": css("--q3") } });
    map.addSource("live", { type: "geojson", data: empty });
    map.addLayer({ id: "live", type: "circle", source: "live", layout: { visibility: "none" }, paint: {} });
    map.addLayer({ id: "countries-line", type: "line", source: "countries", paint: { "line-color": css("--axis"), "line-width": 0.6 } });
    map.addLayer({ id: "country-sel", type: "line", source: "countries", filter: ["==", ["get", "id"], ""], paint: { "line-color": css("--ink"), "line-width": 2.2 } });
    map.addSource("aoi", { type: "geojson", data: empty });
    map.addLayer({ id: "aoi-fill", type: "fill", source: "aoi", paint: { "fill-color": css("--ink"), "fill-opacity": 0.06 } });
    map.addLayer({ id: "aoi-line", type: "line", source: "aoi", paint: { "line-color": css("--ink"), "line-width": 2, "line-dasharray": [2, 1] } });
    styleCountries();
    drawAOI();
    const v = state.aoi && aoiView(state.aoi);
    if (v) map.fitBounds([[v[0], v[1]], [v[2], v[3]]], { padding: 24, maxZoom: 6.5, duration: 0 });
    refreshLayer();
  });

  map.on("moveend", () => { clearTimeout(refreshLayer.t); refreshLayer.t = setTimeout(refreshLayer, 250); });

  // What a map point shows: the country, and the fire square's value explained in plain words.
  // Mouse: on hover, and a click opens the country. Touch screens (phones, tablets) have no hover, so a
  // tap shows the same card, pinned, with an Analyze button (and ✕); dragging the map or tapping
  // elsewhere closes it.
  let lastPointer = "mouse";
  const canvas = map.getCanvas();
  canvas.addEventListener("pointerdown", (ev) => { lastPointer = ev.pointerType || "mouse"; }, { passive: true });
  canvas.addEventListener("touchstart", () => { lastPointer = "touch"; }, { passive: true }); // older iOS without pointer events
  const touchTap = () => lastPointer === "touch" || lastPointer === "pen" || matchMedia("(hover: none)").matches;

  function pointInfo(point) {
    const fs = map.queryRenderedFeatures(point, { layers: ["live", "cells", "countries-fill"].filter((l) => map.getLayer(l)) });
    const cell = fs.find((f) => f.layer.id === "live" || f.layer.id === "cells");
    const ctry = fs.find((f) => f.layer.id === "countries-fill");
    if (!ctry && !cell) return null;
    let html = ctry ? `<b>${esc(ctry.properties.name)}</b>${state.byId[ctry.properties.id]?.ready ? "" : ' <span class="muted">· history loads on first open</span>'}` : "";
    if (cell) {
      const p = cell.properties;
      const word = scaleWord(p.v);
      if (cell.layer.id === "live") {
        html += `<br><b>${fmt(p.v)} detection${p.v === 1 ? "" : "s"}</b> in the last 7 days${word ? ` · ${word}` : ""}`;
        html += `<br><span class="muted">times satellites saw fire in this ≈ 11 km square this week</span>`;
      } else {
        const y = $("mapYear").value, m = +$("mapMonth").value, v = fmt(p.v, 1);
        const [head, line] = y !== "all" ? [`${v} fire days in ${y}`, `satellites saw fire in this ≈ 11 km square on ${v} days of ${y}`]
          : m ? [`${v} fire days in ${MONTHS_LONG[m - 1]}, per year`, `on average each year since 2000, satellites saw fire in this ≈ 11 km square on ${v} days of ${MONTHS_LONG[m - 1]}`]
          : [`${v} fire days a year`, `on average since 2000, satellites saw fire in this ≈ 11 km square on ${v} days each year`];
        html += `<br><b>${head}</b>${word ? ` · ${word}` : ""}<br><span class="muted">${line}</span>`;
      }
      html += `<br><span class="muted">${lat(p.lat)}, ${lon(p.lon)}</span>`;
    }
    return { html, ctry: ctry && { id: ctry.properties.id, name: ctry.properties.name },
             spot: cell && { lat: +cell.properties.lat, lon: +cell.properties.lon } };
  }

  map.on("mousemove", (e) => {
    if (drawing || tipPinned || touchTap()) return;
    const info = pointInfo(e.point);
    map.setFilter("countries-hover", ["==", ["get", "id"], info?.ctry?.id || ""]);
    canvas.style.cursor = info?.ctry ? "pointer" : "";
    if (!info) { hideTip(); return; }
    showTip(e.originalEvent, info.html + `<br><span class="muted">${clickHint(info)}</span>`);
  });
  canvas.addEventListener("mouseleave", () => { if (!tipPinned) { hideTip(); map.setFilter("countries-hover", ["==", ["get", "id"], ""]); } });

  map.on("click", (e) => {
    if (drawing) return;
    const info = pointInfo(e.point);
    if (touchTap()) { // phones and tablets: explain first, analyze on request
      if (!info) { hideTip(); return; }
      map.setFilter("countries-hover", ["==", ["get", "id"], info.ctry?.id || ""]);
      const r = canvas.getBoundingClientRect();
      pinTip({ clientX: r.left + e.point.x, clientY: r.top + e.point.y }, info.html, info.ctry, info.spot);
      return;
    }
    if (!info) return;
    hideTip();
    if (info.spot && (e.originalEvent.shiftKey || !info.ctry)) selectAOI(areaAround(info.spot));
    else if (info.ctry) selectAOI({ country: info.ctry.id });
  });
  map.on("movestart", (e) => { if (e.originalEvent) hideTip(); }); // the user dragged or zoomed the map

  // rectangle drawing (mouse + touch)
  const start = (e) => { if (!drawing) return; e.preventDefault(); dragStart = e.lngLat; };
  const move = (e) => { if (drawing && dragStart) map.getSource("aoi").setData({ type: "FeatureCollection", features: [rectFeature(boundsOf(dragStart, e.lngLat))] }); };
  const end = (e) => {
    if (!drawing || !dragStart) return;
    const b = boundsOf(dragStart, e.lngLat);
    dragStart = null; setDrawing(false);
    if (b[2] - b[0] < 0.2 || b[3] - b[1] < 0.2) { drawAOI(); toast("Drag a larger box (at least 0.2°)"); return; }
    selectAOI({ bbox: b }, { fly: false });
  };
  map.on("mousedown", start); map.on("mousemove", move); map.on("mouseup", end);
  map.on("touchstart", start); map.on("touchmove", move); map.on("touchend", end);
}

function mapUnavailable() {
  map = null;
  $("map").innerHTML = `<div class="map-fallback"><b>Map unavailable on this device</b><span>WebGL is disabled or unsupported. Use the search box or enter coordinates below; every analysis still works.</span></div>`;
  $("draw").disabled = true; $("layerMode").hidden = true;
  document.querySelector(".coords").open = true;
}

function boundsOf(a, b) {
  const r = (v) => Math.round(v * 10) / 10; // snap to the 0.1° analysis grid
  return [r(Math.max(-180, Math.min(a.lng, b.lng))), r(Math.max(-85, Math.min(a.lat, b.lat))),
          r(Math.min(180, Math.max(a.lng, b.lng))), r(Math.min(85, Math.max(a.lat, b.lat)))];
}

function setDrawing(on) {
  drawing = on;
  $("map").classList.toggle("drawing", on);
  $("draw").classList.toggle("active", on);
  $("draw").textContent = on ? "Drag on the map…" : "Draw area";
  if (!map) return;
  if (on) { map.dragPan.disable(); toast("Drag a rectangle on the map (Esc to cancel)"); }
  else { map.dragPan.enable(); }
}

function drawAOI() {
  if (!map || !map.getSource("aoi")) return;
  const a = state.aoi;
  map.getSource("aoi").setData(a?.bbox ? { type: "FeatureCollection", features: [rectFeature(a.bbox)] } : empty);
  map.setFilter("country-sel", ["==", ["get", "id"], a?.country || ""]);
}

function styleCountries() {
  if (!map || !map.getLayer("countries-fill") || !state.meta) return;
  map.setPaintProperty("countries-hover", "fill-color", css("--accent"));
  map.setPaintProperty("countries-line", "line-color", css("--axis"));
  map.setPaintProperty("country-sel", "line-color", css("--ink"));
  map.setPaintProperty("aoi-line", "line-color", css("--ink"));
  map.setPaintProperty("aoi-fill", "fill-color", css("--ink"));
}

function viewBbox() {
  const b = map.getBounds();
  return [Math.max(-180, b.getWest()), Math.max(-90, b.getSouth()), Math.min(180, b.getEast()), Math.min(90, b.getNorth())].map((v) => v.toFixed(2));
}

async function refreshLayer() {
  if (!map || !map.getSource("cells")) return;
  const token = (refreshLayer.token = (refreshLayer.token || 0) + 1);
  const live = state.layer === "live";
  map.setLayoutProperty("cells", "visibility", live ? "none" : "visible");
  map.setLayoutProperty("live", "visibility", live ? "visible" : "none");
  $("histFilters").hidden = live;
  const vb = viewBbox().map(Number), zoom = map.getZoom();
  try {
    if (live) {
      const g = await FireData.live(vb, zoom);
      if (token !== refreshLayer.token) return;
      const d = g.cell;
      map.getSource("live").setData({ type: "FeatureCollection", features: g.cells.map(([xi, yi, v]) => ({
        type: "Feature", properties: { v, lat: yi * d, lon: xi * d },
        geometry: { type: "Point", coordinates: [(xi + 0.5) * d, (yi + 0.5) * d] } })) });
      const max = Math.max(g.max, 2), r = ramp();
      map.setPaintProperty("live", "circle-color", ["interpolate", ["linear"], ["get", "v"], 1, r[3], max, r[6]]);
      map.setPaintProperty("live", "circle-radius", ["interpolate", ["linear"], ["zoom"], 1, 1.6, 4, 3, 8, 7]);
      map.setPaintProperty("live", "circle-stroke-color", css("--surface"));
      map.setPaintProperty("live", "circle-stroke-width", 0.5);
      const days = g.days?.length ? `${niceDate(g.days[0], false)} – ${niceDate(g.days[g.days.length - 1])}` : "";
      mapScale = { max, words: ["few", "some", "many", "very many"] };
      $("mapLegend").innerHTML = mapLegend(r.slice(3), mapScale.words, "1", `${fmt(max)}+`,
        `VIIRS detections per 0.1° cell · ${days}`,
        "<b>Brighter = more fire.</b> Each dot is a ≈ 11 km square, coloured by how many times satellites detected fire there in the last 7 days (provisional data).");
    } else {
      const y = $("mapYear").value, m = +$("mapMonth").value;
      const g = await FireData.grid(vb, y !== "all" ? +y : null, m || null, zoom);
      if (token !== refreshLayer.token) return;
      const d = g.cell;
      map.getSource("cells").setData({ type: "FeatureCollection", features: g.cells.map(([xi, yi, v]) => ({
        type: "Feature", properties: { v, lat: yi * d, lon: xi * d },
        geometry: { type: "Polygon", coordinates: [[[xi * d, yi * d], [(xi + 1) * d, yi * d], [(xi + 1) * d, (yi + 1) * d], [xi * d, (yi + 1) * d], [xi * d, yi * d]]] } })) });
      const max = Math.max(g.max, 0.5), r = ramp();
      const stops = r.slice(1).flatMap((c, i) => [(max * (i + 1)) / (r.length - 1), c]);
      map.setPaintProperty("cells", "fill-color", ["interpolate", ["linear"], ["get", "v"], 0, r[0], ...stops]);
      mapScale = { max, words: ["rare", "occasional", "frequent", "very frequent"] };
      $("mapLegend").innerHTML = g.cells.length
        ? mapLegend(r, mapScale.words, "0", `${fmt(max, 1)}+`,
            y !== "all" ? `fire days in ${y}` : m ? `fire days per year in ${MONTHS_LONG[m - 1]}` : "fire days per year",
            `<b>Brighter = burns more often.</b> Each square (0.1°, ≈ 11 km) is coloured by how many days ${
              y !== "all" ? `in ${esc(y)}` : m ? `of ${MONTHS_LONG[m - 1]}, on average each year,` : "a year, on average since 2000,"
            } satellites saw fire there; one worldwide scale, so colours compare across the globe.`)
        : FireData.mode === "static" ? `<span>No recorded fires in this view for the selected period.</span>`
        : `<span>No history loaded in this view yet. Click a country to load it, or switch to <b>Live</b> for this week's fires worldwide.</span>`;
    }
  } catch (e) {
    if (token === refreshLayer.token) $("mapLegend").textContent = live ? "Live feed is warming up. Try again in a minute." : `Map layer unavailable: ${e.message}`;
  }
}

// the legend's plain word for a map value (rare … very frequent), using the scale currently shown
let mapScale = null;
const scaleWord = (v) => mapScale ? mapScale.words[Math.min(mapScale.words.length - 1, Math.floor((v / mapScale.max) * mapScale.words.length))] : "";

// map legend: the colour bar with plain words along it, the numbers at its ends, and one line of explanation
function mapLegend(colors, words, lo, hi, unit, explain) {
  return `<div class="mlegend">
    <div class="mbar" style="background:linear-gradient(90deg,${colors.join(",")})"></div>
    <div class="mwords">${words.map((w) => `<span>${esc(w)}</span>`).join("")}</div>
    <div class="mends"><span>${esc(lo)}</span><span>${esc(unit)}</span><span>${esc(hi)}</span></div>
    <p class="mexplain">${explain}</p>
  </div>`;
}

function rampLegend(colors, lo, hi, caption) {
  return `<span class="ramp">${esc(lo)}<span class="bar" style="background:linear-gradient(90deg,${colors.join(",")})"></span>${esc(hi)}</span><span>${esc(caption)}</span>`;
}

function refreshBasemap() {
  if (!map || !map.getSource("base")) return;
  map.removeLayer("base"); map.removeSource("base");
  map.addSource("base", { type: "raster", tiles: tiles(), tileSize: 256, attribution: ATTRIB });
  map.addLayer({ id: "base", type: "raster", source: "base" }, "countries-fill");
  styleCountries();
  refreshLayer();
}

// "Just this area": a box about 55 km across centred on the clicked fire square (single ~11 km squares are too
// small for seasonal statistics), kept inside the map's latitude and longitude limits.
const AREA_HALF = 0.25;
function areaAround({ lat: y, lon: x }) {
  const r2 = (v) => Math.round(v * 100) / 100;
  const w = Math.max(-180, x - AREA_HALF), e = Math.min(180, x + AREA_HALF);
  const s = Math.max(-90, y - AREA_HALF), n = Math.min(90, y + AREA_HALF);
  return { bbox: [r2(w), r2(s), r2(e), r2(n)] };
}
function clickHint(info) { // mouse: what a click opens
  if (info.ctry && info.spot) return `Click to analyze ${esc(info.ctry.name)} · Shift-click: just this area`;
  if (info.ctry) return `Click to analyze ${esc(info.ctry.name)}`;
  return "Click to analyze this area";
}
let tipPinned = false;
function showTip(ev, html) {
  const t = $("tip"); t.innerHTML = html; t.style.display = "block";
  t.style.left = "0px"; t.style.top = "0px"; // measure at its natural width first (near an edge it would be squeezed)
  const x = Math.max(8, Math.min(ev.clientX + 14, innerWidth - t.offsetWidth - 8));
  const y = Math.max(8, Math.min(ev.clientY + 14, innerHeight - t.offsetHeight - 8));
  t.style.left = x + "px"; t.style.top = y + "px";
}
function pinTip(ev, html, ctry, spot) { // touch: the card stays, with buttons
  const t = $("tip");
  tipPinned = true; t.classList.add("pinned");
  const buttons = (ctry ? `<button type="button" class="tip-go">Analyze ${esc(ctry.name)}</button>` : "") +
    (spot ? `<button type="button" class="tip-area${ctry ? " ghost" : ""}">Analyze this area <span class="muted">(≈ 55 km)</span></button>` : "");
  showTip(ev, `<button type="button" class="tip-x ghost" aria-label="Close">✕</button>${html}` +
    (buttons ? `<div class="tip-actions">${buttons}</div>` : ""));
  t.querySelector(".tip-x").onclick = hideTip;
  const go = t.querySelector(".tip-go");
  if (go) go.onclick = () => { hideTip(); selectAOI({ country: ctry.id }); };
  const area = t.querySelector(".tip-area");
  if (area) area.onclick = () => { hideTip(); selectAOI(areaAround(spot)); };
}
function hideTip() {
  const t = $("tip"); t.style.display = "none"; t.classList.remove("pinned"); tipPinned = false;
  map?.getLayer("countries-hover") && map.setFilter("countries-hover", ["==", ["get", "id"], ""]);
}
// a tap anywhere outside the pinned card closes it
document.addEventListener("pointerdown", (ev) => { if (tipPinned && !$("tip").contains(ev.target) && !ev.target.closest?.("#map")) hideTip(); }, { passive: true });

// ───────────────────────── loading an AOI ─────────────────────────
function setBusy(on, msg) {
  $("content").setAttribute("aria-busy", String(on));
  if (msg) $("loading").innerHTML = `<div class="loading-box">${msg}</div>`;
  if (!on) syncNav();
}

function banner(html, kind = "info") {
  const b = $("banner");
  if (!html) { b.hidden = true; b.innerHTML = ""; return; }
  b.hidden = false; b.className = `banner ${kind}`; b.innerHTML = html;
}

async function loadAOI() {
  const a = state.aoi, token = ++state.req;
  const name = a.country ? state.byId[a.country].name : `${lat(a.bbox[1])} – ${lat(a.bbox[3])}, ${lon(a.bbox[0])} – ${lon(a.bbox[2])}`;
  $("aoiTitle").textContent = name; $("navArea").textContent = name;
  $("aoiSub").textContent = a.country ? "Country" : "Custom area";
  banner(null);
  setBusy(true, `<span class="spinner"></span> Analyzing ${esc(name)}…`);
  state.now = null; state.nowLoading = true; renderNowcast();
  if (FireData.mode === "server") loadNowcast(a, token); // static: needs the history first (below)
  if (a.bbox) {
    const area = (a.bbox[2] - a.bbox[0]) * (a.bbox[3] - a.bbox[1]);
    if (area > FireData.maxBoxDeg2) {
      setBusy(false); $("results").hidden = true; state.nowLoading = false; renderNowcast();
      banner(`This box covers ${fmt(area)} square degrees; the largest custom area here is ${fmt(FireData.maxBoxDeg2)} (about ${fmt(Math.sqrt(FireData.maxBoxDeg2))}° × ${fmt(Math.sqrt(FireData.maxBoxDeg2))}°). Draw a smaller box, or pick a country for larger regions.`, "warn");
      return;
    }
  }
  const na = a.country && state.byId[a.country]?.unavailable;
  if (na) { // NASA has no usable fire archive for this country: say so instead of trying to load it
    setBusy(false); $("results").hidden = true; state.data = null; state.nowLoading = false; renderNowcast();
    banner(`NASA FIRMS has no usable fire archive for ${esc(state.byId[a.country].name)}, so it can't be harmonized.`, "warn");
    return;
  }
  const progress = (done, total, bytes, totalBytes) => {
    if (token === state.req && total) setBusy(true, `<span class="spinner"></span> Reading fire records for this area… ${done}/${total} tiles · ${fmt(bytes / 1e6, 1)} of ${fmt(totalBytes / 1e6, 1)} MB`);
  };
  try {
    const data = await FireData.calendar(a, progress);
    if (token !== state.req) return;
    if (data.unavailable) {
      state.data = null; $("results").hidden = true; setBusy(false); state.nowLoading = false;
      banner(esc(data.detail), "warn"); loadNowcast(a, token, null); return;
    }
    if (data.needs_data) {
      if (!data.missing.length) {
        state.data = null; $("results").hidden = true; setBusy(false);
        banner("No land with fire records in this box. Try drawing over land.", "warn");
      } else prepare(data.missing.map((m) => m.id), token);
      return;
    }
    state.data = data;
    showResults();
    if (FireData.mode === "static") loadNowcast(a, token, data);
    if (data.missing.length && FireData.mode === "static") {
      banner(`<b>Partial coverage.</b> This box also covers ${data.missing.map((m) => esc(m.name)).join(", ")}, whose record isn't published yet, so fires there are not counted.`, "warn");
    } else if (data.missing.length && FireData.mode === "server") {
      banner(`<b>Partial coverage.</b> This box also covers ${data.missing.map((m) => esc(m.name)).join(", ")}, which ${data.missing.length > 1 ? "haven't" : "hasn't"} been loaded yet.
        <button type="button" id="loadMissing">Load ${data.missing.length > 1 ? "them" : "it"}</button>`, "warn");
      $("loadMissing").onclick = () => prepare(data.missing.map((m) => m.id), token);
    }
    setBusy(false);
  } catch (e) {
    if (token !== state.req) return;
    $("results").hidden = true;
    setBusy(false); state.nowLoading = false; renderNowcast();
    banner(`Could not analyze this area: ${esc(e.message)} <button type="button" id="retry">Retry</button>`, "error");
    $("retry").onclick = () => { state.data = null; loadAOI(); };
  }
}

// first-time processing of a country's archive, with live progress
async function prepare(ids, token) {
  if (!state.data || state.data.aoi && aoiKey(state.data.aoi) !== aoiKey(state.aoi)) { state.data = null; $("results").hidden = true; }
  setBusy(false);
  try {
    await FireData.prepare(ids);
  } catch (e) {
    banner(`Could not start loading: ${esc(e.message)}`, "error"); return;
  }
  const names = ids.map((i) => state.byId[i]?.name || i);
  const label = (j) => ({ queued: "Waiting in queue", downloading: `Downloading ${j.done || 0} of ${j.total || "…"} yearly files`, building: "Harmonizing…", ready: "Ready", error: "Failed" }[j.state] || j.state);
  const poll = async () => {
    if (token !== state.req) return;
    let jobs = {};
    try { jobs = await FireData.jobs(); } catch (_) {}
    const rows = ids.map((id) => {
      const j = jobs[id] || { state: "ready" };
      const pct = j.state === "downloading" && j.total ? Math.round((j.done / j.total) * 90) : j.state === "building" ? 95 : j.state === "ready" ? 100 : 2;
      return { id, j, pct };
    });
    const failed = rows.filter((r) => r.j.state === "error");
    const done = rows.every((r) => r.j.state === "ready" || r.j.state === "error");
    banner(`<b>Loading ${esc(names.join(", "))} for the first time.</b> FireCal is downloading 24 years of NASA FIRMS hotspots and harmonizing them. This only happens once; afterwards it opens instantly. Meanwhile, this week's live fires are shown above.
      ${rows.map((r) => `<div class="progress"><span>${esc(state.byId[r.id]?.name || r.id)}</span>
        <span class="pbar" role="progressbar" aria-valuenow="${r.pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${r.pct}%"></i></span><span class="pstate">${esc(label(r.j))}</span></div>`).join("")}`, failed.length ? "error" : "info");
    if (done) {
      await loadMeta();
      if (failed.length) banner(`Could not load ${failed.map((f) => esc(state.byId[f.id]?.name)).join(", ")}: ${esc(failed[0].j.message)}`, "error");
      if (failed.length < rows.length) { state.data = null; loadAOI(); refreshLayer(); }
      return;
    }
    setTimeout(poll, 2000);
  };
  poll();
}

async function loadNowcast(a, token, cal) {
  try {
    const n = await FireData.nowcast(a, cal);
    if (token !== state.req) return;
    state.now = n.needs_data ? null : n;
  } catch (_) { state.now = null; }
  state.nowLoading = false;
  renderNowcast();
  if (state.data) renderBriefing();
}

function showResults() {
  const d = state.data;
  $("results").hidden = false;
  $("aoiTitle").textContent = d.label; $("navArea").textContent = d.label;
  const cs = d.countries.map((c) => c.name);
  $("aoiSub").textContent = state.aoi.country
    ? `Country · ${fmt(d.total_cell_days)} fire cell-days recorded since Nov 2000`
    : `Custom area across ${cs.join(", ")} · ${fmt(d.total_cell_days)} fire cell-days since Nov 2000`;
  const sel = $("season");
  sel.innerHTML = d.seasons.map((s, i) => `<option value="${i}">${s.label}</option>`).join("");
  state.season = d.seasons.length - 1; sel.value = state.season;
  const years = d.monthly.years;
  $("dayYear").innerHTML = years.slice().reverse().map((y) => `<option>${y}</option>`).join("");
  if (!years.includes(+state.dayYear)) state.dayYear = years[years.length - 1];
  $("dayYear").value = state.dayYear;
  renderAll();
}

// ───────────────────────── lazy chart rendering ─────────────────────────
const renderers = {};
const dirty = new Set();
const visible = new Set();
let io = null;
function markAllDirty() { Object.keys(renderers).forEach((k) => dirty.add(k)); flush(); }
function flush() {
  if (!state.data || !window.echarts) return;
  for (const id of [...dirty]) if (visible.has(id) || !io) { dirty.delete(id); try { renderers[id](); } catch (e) { console.error(id, e); } }
}
function setupLazy() {
  document.querySelectorAll("[data-render]").forEach((el) => { renderers[el.id] = RENDER[el.dataset.render]; });
  if (!("IntersectionObserver" in window)) return;
  io = new IntersectionObserver((entries) => {
    for (const en of entries) { if (en.isIntersecting) visible.add(en.target.id); else visible.delete(en.target.id); }
    flush();
  }, { rootMargin: "400px 0px" });
  document.querySelectorAll("[data-render]").forEach((el) => io.observe(el));
}

// ───────────────────────── section navigation ─────────────────────────
function setupNav() {
  const nav = $("secnav"), links = [...nav.querySelectorAll("a[data-sec]")];
  let lockUntil = 0, current = null;
  const setOn = (id) => {
    if (id === current) return;
    current = id;
    for (const l of links) {
      const on = l.dataset.sec === id;
      l.classList.toggle("on", on);
      if (on) {
        l.setAttribute("aria-current", "true");
        // keep the active chip visible inside the menu; instant, and only when needed, so it never
        // interrupts the page's own smooth scroll
        if (l.offsetLeft < nav.scrollLeft || l.offsetLeft + l.offsetWidth > nav.scrollLeft + nav.clientWidth)
          nav.scrollLeft = Math.max(0, l.offsetLeft - (nav.clientWidth - l.offsetWidth) / 2);
      } else l.removeAttribute("aria-current");
    }
  };
  const offset = () => nav.getBoundingClientRect().bottom + 24;
  const topbar = document.querySelector(".topbar"), wrap = $("secnavWrap");
  const measure = () => document.documentElement.style.setProperty("--topbar-h", `${topbar.offsetHeight}px`);
  measure();
  addEventListener("resize", measure);
  const spy = () => {
    wrap.classList.toggle("stuck", wrap.getBoundingClientRect().top <= topbar.offsetHeight + 1);
    if (performance.now() < lockUntil) return;
    const line = offset();
    let active = null;
    for (const l of links) {
      const el = $(l.dataset.sec);
      if (!el || el.hidden || l.hidden) continue;
      if (el.getBoundingClientRect().top <= line) active = l.dataset.sec; // last section whose top has passed the menu
    }
    if (innerHeight + scrollY >= document.documentElement.scrollHeight - 4) { const shown = links.filter((l) => !l.hidden); active = shown[shown.length - 1].dataset.sec; }
    setOn(active || links.find((l) => !l.hidden)?.dataset.sec);
  };
  let ticking = false;
  addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(() => { ticking = false; spy(); }); } }, { passive: true });
  addEventListener("resize", spy);
  nav.addEventListener("click", (e) => {
    const a = e.target.closest("a"); if (!a) return;
    e.preventDefault();
    if (a.dataset.top !== undefined) { scrollTo({ top: 0, behavior: "smooth" }); return; }
    const el = $(a.dataset.sec);
    if (!el || el.hidden) return;
    setOn(a.dataset.sec);
    lockUntil = performance.now() + 1250; // don't let the spy flicker during the smooth scroll
    // land just below the stuck menu: header + menu band + a small gap
    const target = () => Math.max(0, scrollY + el.getBoundingClientRect().top - (topbar.offsetHeight + wrap.offsetHeight + 12));
    scrollTo({ top: target(), behavior: "smooth" });
    // content above can still grow while we scroll (live data arriving): settle on the exact spot
    setTimeout(() => { const y = target(); if (Math.abs(y - scrollY) > 6) scrollTo({ top: y, behavior: "smooth" }); }, 750);
    setTimeout(spy, 1300);
  });
  setupNav.spy = spy;
}
function syncNav() {
  $("secnav").querySelector('[data-sec="nowCard"]').hidden = $("nowCard").hidden;
  $("secnav").classList.toggle("off", $("results").hidden);
  setupNav.spy?.();
}

function renderAll() {
  if (!state.data) return;
  renderKPIs(); renderBriefing(); renderCritical(); renderUnusual(); renderCV(); sizeDaily();
  markAllDirty();
  syncNav();
}

function base() {
  return {
    animationDuration: 300,
    textStyle: { fontFamily: 'Inter, system-ui, -apple-system, "Segoe UI", sans-serif', color: css("--ink-2"), fontSize: 12 },
    tooltip: {
      confine: true, backgroundColor: css("--surface"), borderColor: css("--border"), borderWidth: 1,
      textStyle: { color: css("--ink"), fontSize: 12 }, extraCssText: "box-shadow:0 12px 32px -8px rgba(0,0,0,.35);border-radius:12px;",
    },
  };
}
const axisCommon = () => ({
  axisLine: { lineStyle: { color: css("--axis") } }, axisTick: { show: false },
  axisLabel: { color: css("--muted"), fontSize: 11 }, splitLine: { lineStyle: { color: css("--grid") } },
});
function chart(id) {
  if (!charts[id]) charts[id] = echarts.init($(id), null, { renderer: "canvas" });
  return charts[id];
}

function sourceOf(iso) {
  const p = state.data.harmonization.periods;
  if (iso < p[1].from) return "MODIS Terra only × k";
  if (iso < p[2].from) return "MODIS Terra+Aqua × k";
  return "VIIRS S-NPP";
}
function sensorTag(year) {
  const p = state.data.harmonization.periods, a = +p[1].from.slice(0, 4), v = +p[2].from.slice(0, 4);
  if (year < a) return "T";
  if (year < v) return year === a ? "T/A" : "T+A";
  return "V";
}

// ───────────────────────── early warning ─────────────────────────
function nowStatus(n) {
  if (!n?.history) return null;
  if (n.record) return { label: "Highest for these dates since 2001", color: css("--critical"), level: 3 };
  if (n.percentile >= 90) return { label: "Well above normal", color: css("--serious"), level: 2 };
  if (n.percentile >= 70) return { label: "Above normal", color: css("--warning"), level: 1 };
  if (n.percentile <= 10) return { label: "Well below normal", color: css("--good"), level: 0 };
  return { label: "Near normal", color: css("--good"), level: 0 };
}
function renderNowcast() {
  const n = state.now;
  if (state.nowLoading) { // reserve the card's space so the page doesn't jump when live data lands
    $("nowCard").hidden = false; $("nowCard").classList.add("is-loading");
    $("nowCard").style.setProperty("--now-c", css("--accent"));
    $("nowSub").innerHTML = `<span class="spinner"></span>Checking this week's live NASA fire detections…`;
    $("nowStatus").innerHTML = "";
    $("nowFigs").innerHTML = ["This week", "Typical for these dates", "Rank"].map((l) => `<div><span>${l}</span><b class="skel">&nbsp;</b><em>&nbsp;</em></div>`).join("");
    $("nowBars").innerHTML = Array.from({ length: 6 }, () => `<div class="nb"><b>&nbsp;</b><i class="skel" style="height:40%"></i><span>&nbsp;</span></div>`).join("");
    syncNav(); return;
  }
  $("nowCard").classList.remove("is-loading");
  if (!n || !n.available) { $("nowCard").hidden = true; syncNav(); return; }
  $("nowCard").hidden = false; syncNav();
  const d0 = n.days[0], d1 = n.days[n.days.length - 1];
  $("nowSub").textContent = `${niceDate(d0, false)} – ${niceDate(d1)} · provisional VIIRS near-real-time · updated ${n.fetched_at ? new Date(n.fetched_at).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "–"}`;
  if (n.direct_from_nasa) $("nowSub").insertAdjacentHTML("beforeend", ` <span class="muted">· read directly from NASA (the published copy is behind)</span>`);
  // say so plainly if the live data is old (NASA's own update time, else when it was fetched)
  const since = n.source_updated || n.fetched_at, ageH = since ? (Date.now() - new Date(since)) / 36e5 : 0;
  if (ageH > 6) $("nowSub").insertAdjacentHTML("beforeend", ` <span class="stale">· ⚠ not refreshed for ${fmt(ageH)} hours; this week's figures may be out of date</span>`);
  const st = nowStatus(n);
  $("nowCard").style.setProperty("--now-c", st ? st.color : css("--accent"));
  $("nowStatus").innerHTML = st ? `<div class="status big"><i style="background:${st.color}"></i>${esc(st.label)}</div>` : "";
  $("nowFigs").innerHTML = n.history
    ? `<div><span>This week</span><b>${fmt(n.total)}</b><em>fire cell-days</em></div>
       <div><span>Typical for these dates</span><b>${fmt(n.p50)}</b><em>normal range ${fmt(n.p10)}–${fmt(n.p90)}</em></div>
       <div><span>Rank</span><b>${ordinal(n.percentile).replace(/(\d+)(\D+)/, "$1<small>$2</small>")}</b><em>percentile of ${n.n_years} years</em></div>`
    : `<div><span>This week</span><b>${fmt(n.total)}</b><em>fire cell-days</em></div>
       <div class="wide"><span>History loading</span><em>The comparison with past years appears once this area's archive is ready.</em></div>`;
  const max = Math.max(1, ...n.cells);
  $("nowBars").innerHTML = n.cells.map((v, i) => `<div class="nb" title="${niceDate(n.days[i])}: ${fmt(v)} fire cell-days">
      <b>${fmt(v)}</b><i style="height:${Math.max(3, (v / max) * 100)}%"></i><span>${new Date(n.days[i] + "T00:00:00Z").toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" })}</span></div>`).join("");
}

// ───────────────────────── KPIs ─────────────────────────
function confidenceIssues(h) { // why results are "indicative only" (mirrors low_counts in analysis.py)
  const out = [];
  if (h.overlap_viirs_cell_days < 3000) out.push({ short: "few fires", long: `only ${fmt(h.overlap_viirs_cell_days)} VIIRS fire cell-days in the 2012+ overlap years, so the calibration leans on the worldwide ratio` });
  if (h.r2_monthly == null || h.r2_monthly < 0.5) out.push({ short: "noisy monthly fit", long: `the month-by-month fit is weak (R² ${h.r2_monthly ?? "n/a"})` });
  if (h.cv_median_ape == null || h.cv_median_ape > 15) out.push({ short: "large test error", long: `predicting held-out years misses by ${h.cv_median_ape ?? "an unknown"}% (median)` });
  return out;
}

function statusOf(z) {
  if (z == null) return { label: "No data", color: css("--muted") };
  if (z >= 2) return { label: "Extreme, well above normal", color: css("--critical") };
  if (z >= 1) return { label: "Above normal", color: css("--serious") };
  if (z <= -1) return { label: "Below normal", color: css("--warning") };
  return { label: "Near normal", color: css("--good") };
}

function renderKPIs() {
  const d = state.data, c = d.critical, h = d.harmonization;
  const last = d.seasons[d.seasons.length - 1];
  const mean = d.seasons.reduce((a, s) => a + s.total, 0) / Math.max(1, d.seasons.length);
  const top = d.unusual[0];
  const tiles = [
    { label: "Typical fire season", value: c ? `${c.start} – ${c.end}` : "–", note: c ? `${c.length} days · peak around ${c.peak}` : "Too little fire activity to define" },
    { label: `Latest full season (${last?.label ?? "–"})`, value: last && mean > 0 ? `${signed(((last.total - mean) / mean) * 100, 0)}%` : "–",
      note: `vs long-term mean · ${fmt(last?.total)} cell-days`, status: statusOf(last?.z) },
    { label: "Most unusual month", value: top ? `${MONTHS[top.month - 1]} ${top.year}` : "None", note: top ? `${signed(top.z)} σ from normal (${top.z > 0 ? "more" : "less"} burning)` : "No month beyond ±2σ" },
    { label: "Harmonization skill", value: h.cv_median_ape != null ? `±${h.cv_median_ape}%` : "–",
      note: `median out-of-sample error · 1 MODIS ≈ ${fmt(h.k_all, 2)} VIIRS cell-days`,
      status: h.low_counts ? { label: `Indicative only: ${confidenceIssues(h).map((i) => i.short).join(", ")}`, color: css("--warning") } : { label: "Robust calibration", color: css("--good") } },
  ];
  $("kpis").innerHTML = tiles.map((t) => `
    <div class="kpi"><div class="label">${esc(t.label)}</div><div class="value">${esc(t.value)}</div>
    <div class="note">${esc(t.note)}</div>
    ${t.status ? `<div class="status"><i style="background:${t.status.color}"></i>${esc(t.status.label)}</div>` : ""}</div>`).join("");
}

// ───────────────────────── plain-language briefing ─────────────────────────
function outlook() {
  // climatological change over the next 30 days vs the last 30, from today's date
  const cl = state.data.climatology, at = Object.fromEntries(cl.keys.map((k, i) => [k, i]));
  const today = new Date();
  const val = (off) => { const t = new Date(Date.UTC(2001, today.getUTCMonth(), today.getUTCDate() + off)); return cl.mean[at[isoOf(t).slice(5)]] ?? 0; };
  let past = 0, next = 0;
  for (let i = 1; i <= 30; i++) { past += val(-i); next += val(i); }
  return { past, next, change: past > 0 ? ((next - past) / past) * 100 : null };
}

function renderBriefing() {
  const d = state.data;
  if (!d) return;
  const c = d.critical, h = d.harmonization, n = state.now, st = nowStatus(n);
  const mu = d.monthly.normal, total = mu.reduce((a, v) => a + (v || 0), 0);
  const ranked = mu.map((v, i) => [v || 0, i]).sort((a, b) => b[0] - a[0]);
  const busiest = ranked.slice(0, 3).filter(([v]) => v > 0).map(([, i]) => MONTHS_LONG[i]); // months that burn at all
  const quiet = ranked.slice(-3).reverse().map(([, i]) => MONTHS_LONG[i]);
  const busyShare = total ? (ranked.slice(0, 3).reduce((a, r) => a + r[0], 0) / total) * 100 : 0;
  const none = d.total_cell_days === 0; // e.g. the Maldives: no fire recorded by either sensor since 2000
  const ol = outlook();
  const yr = d.yearly, fit = linfit(yr.years, yr.h);
  const trend = fit && fit.mean > 0 ? (fit.slope * 10 / fit.mean) * 100 : null;
  const timed = d.seasons.filter((s) => s.start_off != null);
  const sfit = linfit(timed.map((s) => s.start_year), timed.map((s) => s.start_off));
  const lfit = linfit(timed.map((s) => s.start_year), timed.map((s) => s.length));
  const recent = d.unusual.filter((u) => u.year >= d.range.last_full - 4);
  const li = (html) => `<li>${html}</li>`;
  const lc = h.low_counts ? ` <span class="muted">(low confidence: ${confidenceIssues(h).map((i) => i.short).join(", ")})</span>` : "";
  const now = new Date(), monthName = MONTHS_LONG[now.getUTCMonth()];
  const olText = ol.change == null || (ol.past + ol.next) < 1 ? "Little burning is normally recorded around this time of year."
    : Math.abs(ol.change) < 15 ? `Burning normally stays <b>about level</b> over the next 30 days.`
    : ol.change >= 100 ? `Burning normally becomes <b>~${fmt(ol.next / ol.past, 1)}× higher</b> over the next 30 days: prepare now.`
    : ol.change > 0 ? `Burning normally <b>rises ~${fmt(ol.change)}%</b> over the next 30 days: prepare now.`
    : `Burning normally <b>falls ~${fmt(-ol.change)}%</b> over the next 30 days.`;

  const B = {
    responders: [
      state.nowLoading ? li(`<b>Right now:</b> <span class="muted">checking this week's live fires…</span>`) :
      st ? li(`<b>Right now:</b> ${fmt(n.total)} fire cell-day${n.total === 1 ? "" : "s"} this week, <b>${esc(st.label.toLowerCase())}</b> (${ordinal(n.percentile)} percentile for these dates).`) : "",
      li(`<b>Outlook from ${monthName}:</b> ${olText}`),
      c ? li(`<b>Critical period:</b> ${c.start} → ${c.end}, peaking around <b>${c.peak}</b>. Highest-risk weeks: ${d.top_weeks.map(esc).join(", ")}.`) : none ? "" : li("No regular fire season: burning here is sporadic."),
      none ? li("<b>No fires recorded</b> here by MODIS or VIIRS since November 2000.")
        : busiest.length ? li(`<b>Busiest months:</b> ${busiest.join(", ")} (${fmt(busyShare)}% of a normal year's burning).`) : "",
    ],
    managers: [
      c ? li(`<b>Plan around the season:</b> ${c.length} days long on average (${c.start} – ${c.end}); ${lfit ? `it has been getting <b>${lfit.slope > 0 ? "longer" : "shorter"}</b> by ~${fmt(Math.abs(lfit.slope * 10))} days per decade${lc}.` : ""}`) : "",
      sfit ? li(`<b>Season onset</b> is shifting <b>${sfit.slope < 0 ? "earlier" : "later"}</b> by ~${fmt(Math.abs(sfit.slope * 10))} days per decade${lc}.`) : "",
      none ? li("<b>No fires recorded</b> here by MODIS or VIIRS since November 2000, so there is no fire season to plan around.")
        : li(`<b>Quietest months</b> (windows for fuel management and prescribed burning, subject to local rules): ${quiet.join(", ")}.`),
      trend != null ? li(`<b>Long-term trend:</b> annual burning is ${Math.abs(trend) < 5 ? "roughly stable" : trend > 0 ? `<b>rising ~${fmt(trend)}%</b>` : `<b>falling ~${fmt(-trend)}%</b>`} per decade (harmonized 2001–${d.range.last_full})${lc}.`) : "",
      recent.length ? li(`<b>Recent unusual months:</b> ${recent.slice(0, 4).map((u) => `${MONTHS[u.month - 1]} ${u.year} (${signed(u.z)}σ)`).join(", ")}.`) : li("No unusual months (±2σ) in the last five years."),
    ],
    scientists: [
      li(`<b>Harmonization:</b> MODIS → VIIRS-equivalent with k = ${h.k_all} (Terra-only ${h.k_terra_all}); monthly R² ${h.r2_monthly ?? "–"}; leave-one-year-out median error ${h.cv_median_ape ?? "–"}% (Terra-only ${h.cv_median_ape_terra ?? "–"}%).`),
      li(`<b>Independent check:</b> Terra-only and Terra+Aqua reconstructions agree within ${h.terra_check_ape ?? "–"}% (2003–2011).`),
      li(`<b>Sample:</b> ${fmt(h.overlap_viirs_cell_days)} VIIRS fire cell-days in the overlap years (worldwide prior k = ${h.k_world})${h.low_counts ? `. <b>Indicative only:</b> ${confidenceIssues(h).map((i) => i.long).join("; ")}` : ""}.`),
      trend != null ? li(`<b>Trend:</b> ${signed(trend, 1)}% per decade in annual fire cell-days (OLS, ${yr.years[0]}–${yr.years[yr.years.length - 1]}).`) : "",
      FireData.mode === "server"
        ? li(`<b>Reuse:</b> download the daily series (CSV) or query <code>api/calendar?${esc(aoiQuery(state.aoi))}</code>. <a href="docs" target="_blank" rel="noopener">API docs</a>.`)
        : li(`<b>Reuse:</b> download the daily series (CSV)${state.aoi.country ? `, or the full analysis as <a href="data/countries/${encodeURIComponent(state.aoi.country)}.json" target="_blank" rel="noopener">JSON</a>` : " (this area was analysed in your browser from the raw 0.1° fire records)"}. Source code: <a href="https://github.com/samuelakosaonyejekwe/firecal" target="_blank" rel="noopener">GitHub</a>.`),
    ],
  };
  $("briefList").className = `brief ${state.audience}`;
  $("briefList").innerHTML = B[state.audience].join("");
}

// ───────────────────────── charts ─────────────────────────
function renderCalendar() {
  const d = state.data, m = d.monthly, zMode = state.calMode === "z";
  const years = m.years, data = [];
  years.forEach((y, yi) => MONTHS.forEach((_, mo) => {
    const v = (zMode ? m.z : m.values)[yi][mo];
    if (m.values[yi][mo] != null) data.push([mo, yi, v == null ? "-" : v]);
  }));
  const vmax = zMode ? 3 : Math.max(1, quantile(m.values.flat(), 0.97));
  const colors = zMode ? diverging() : ramp(), muted = css("--muted");
  chart("chCalendar").setOption({
    ...base(),
    grid: { left: 70, right: 8, top: 8, bottom: 26 },
    xAxis: { type: "category", data: MONTHS, ...axisCommon(), axisLine: { show: false }, splitLine: { show: false } },
    yAxis: { type: "category", data: years.map(String), inverse: true, ...axisCommon(), axisLine: { show: false }, splitLine: { show: false },
      axisLabel: { color: css("--ink-2"), fontSize: 11, formatter: (y) => `${y} {t|${sensorTag(+y)}}`, rich: { t: { color: muted, fontSize: 10, width: 22 } } } },
    visualMap: { show: false, min: zMode ? -3 : 0, max: vmax, dimension: 2, inRange: { color: colors } },
    tooltip: { ...base().tooltip, formatter: (p) => {
      const [mo, yi] = p.data, y = years[yi];
      return `<b>${MONTHS[mo]} ${y}</b><br>${fmt(m.values[yi][mo])} fire cell-days<br>Normal ${fmt(m.normal[mo])} · ${signed(m.z[yi][mo])} σ<br>` +
             `<span style="color:${muted}">Source: ${sourceOf(`${y}-${String(mo + 1).padStart(2, "0")}-15`)}</span>`; } },
    series: [{ type: "heatmap", data, itemStyle: { borderColor: css("--surface"), borderWidth: 2, borderRadius: 3 },
               emphasis: { itemStyle: { borderColor: css("--ink"), borderWidth: 1.5 } } }],
  }, true);
  chart("chCalendar").off("click");
  chart("chCalendar").on("click", (p) => { // drill down to the daily calendar of that year
    state.dayYear = years[p.data[1]]; $("dayYear").value = state.dayYear; dirty.add("chDaily"); flush();
    $("dailyCard").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $("calSub").textContent = zMode
    ? "How unusual each month was vs its long-term normal (σ). Tap a row to open that year day by day."
    : "Fire cell-days per month, VIIRS-equivalent. T = Terra, T+A = Terra+Aqua, V = VIIRS. Tap a row to open that year.";
  $("calLegend").innerHTML = zMode ? rampLegend(colors, "−3σ less", "+3σ more", "burning vs normal for that month")
                                   : rampLegend(colors, "0", `${fmt(vmax)}+`, "fire cell-days per month");
}

function renderCritical() {
  const d = state.data, c = d.critical, s0 = d.season_start_month;
  if (!c) { $("critical").innerHTML = `<p class="empty">Too little fire activity in this area to define a season.</p>`; return; }
  const pct = (off) => (off / 365) * 100;
  const axis = Array.from({ length: 12 }, (_, i) => MONTHS[(s0 - 1 + i) % 12]);
  // today's position within the season year
  const t = new Date(), y0 = t.getUTCMonth() + 1 >= s0 ? t.getUTCFullYear() : t.getUTCFullYear() - 1;
  const todayOff = (Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate()) - Date.UTC(y0, s0 - 1, 1)) / 864e5;
  $("critical").innerHTML = `
    <div class="crit-bar" id="critBar">
      <div class="span" style="left:${pct(c.start_off)}%;width:${pct(c.end_off - c.start_off)}%"></div>
      <div class="pk" style="left:${pct(c.peak_off)}%"></div>
      <div class="today" style="left:${pct(todayOff)}%"><span>Today</span></div>
    </div>
    <div class="crit-axis">${axis.map((m) => `<span>${m}</span>`).join("")}</div>
    <div class="crit-facts">
      <div><span>Season opens</span><b>${c.start}</b></div>
      <div><span>Peak burning</span><b>${c.peak}</b></div>
      <div><span>Season closes</span><b>${c.end}</b></div>
    </div>
    <p class="weeks">Highest-risk weeks: <b>${d.top_weeks.map(esc).join(" · ")}</b>. The shaded band holds the central 80% of each season's burning; the dot line marks today.</p>`;
  $("critBar").addEventListener("mousemove", (e) => showTip(e, `Typical season: <b>${c.start} → ${c.end}</b><br>Peak ≈ ${c.peak} · ${c.length} days`));
  $("critBar").addEventListener("mouseleave", hideTip);
}

function renderUnusual() {
  const u = state.data.unusual;
  if (!u.length) { $("unusual").innerHTML = `<p class="empty">No month in this area deviates by 2σ or more.</p>`; return; }
  $("unusual").innerHTML = `<table><thead><tr><th>Month</th><th class="num">Fire cell-days</th><th class="num">Normal</th><th class="num">Anomaly</th></tr></thead><tbody>
    ${u.map((r) => `<tr><td>${MONTHS[r.month - 1]} ${r.year}</td><td class="num">${fmt(r.value)}</td><td class="num">${fmt(r.normal)}</td>
      <td class="num"><span class="pill ${r.z > 0 ? "hi" : "lo"}">${r.z > 0 ? "▲" : "▼"} ${signed(r.z)} σ</span></td></tr>`).join("")}
  </tbody></table>`;
}

function seasonKeys() {
  const s0 = state.data.season_start_month, keys = state.data.climatology.keys;
  const i0 = keys.findIndex((k) => +k.slice(0, 2) === s0);
  return keys.slice(i0).concat(keys.slice(0, i0));
}
function renderProfile() {
  const d = state.data, cl = d.climatology, keys = seasonKeys();
  const at = Object.fromEntries(cl.keys.map((k, i) => [k, i]));
  const pick = (arr) => keys.map((k) => arr[at[k]]);
  const p10 = pick(cl.p10), p90 = pick(cl.p90), p50 = pick(cl.p50);
  const s = d.seasons[state.season];
  if (!s) return;
  const i0 = Math.round((new Date(s.from) - new Date(d.daily.start)) / 864e5);
  const byKey = {};
  for (let i = 0; i < 366; i++) {
    const iso = isoOf(addDays(d.daily.start, i0 + i));
    if (iso.slice(5) !== "02-29") byKey[iso.slice(5)] = d.daily.h7[i0 + i];
  }
  const sel = keys.map((k) => byKey[k] ?? null);
  const label = (k) => `${+k.slice(3)} ${MONTHS[+k.slice(0, 2) - 1]}`;
  const band = css("--q1"), sc = css("--s-harm");
  chart("chProfile").setOption({
    ...base(),
    grid: { left: 56, right: 16, top: 34, bottom: 28 },
    tooltip: { ...base().tooltip, trigger: "axis", axisPointer: { type: "line", lineStyle: { color: css("--axis") } },
      formatter: (ps) => { const i = ps[0].dataIndex;
        return `<b>${label(keys[i])}</b><br>Season ${s.label}: <b>${fmt(sel[i], 1)}</b><br>Median: ${fmt(p50[i], 1)}<br>Normal range: ${fmt(p10[i], 1)}–${fmt(p90[i], 1)}`; } },
    xAxis: { type: "category", data: keys, boundaryGap: false, ...axisCommon(), splitLine: { show: false },
             axisLabel: { color: css("--muted"), fontSize: 11, interval: (i, k) => k.endsWith("-01"), formatter: (k) => MONTHS[+k.slice(0, 2) - 1] } },
    yAxis: { type: "value", ...axisCommon(), axisLine: { show: false }, name: "cell-days / day", nameTextStyle: { color: css("--muted"), align: "left" } },
    series: [
      { name: "p10", type: "line", data: p10, stack: "band", symbol: "none", lineStyle: { opacity: 0 }, silent: true },
      { name: "range", type: "line", data: p90.map((v, i) => v - p10[i]), stack: "band", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: band, opacity: 0.8 } },
      { name: "Median", type: "line", data: p50, symbol: "none", lineStyle: { width: 1.5, type: "dashed", color: css("--ink-2") } },
      { name: "Season", type: "line", data: sel, symbol: "none", lineStyle: { width: 2, color: sc } },
    ],
  }, true);
  $("profLegend").innerHTML = `<span class="key"><span class="sw" style="background:${band}"></span>Normal range (10–90%)</span>` +
    `<span class="key"><span class="ln dash" style="border-color:${css("--ink-2")}"></span>Median</span>` +
    `<span class="key"><span class="ln" style="border-color:${sc}"></span>Season ${esc(s.label)}</span>`;
}

// the daily calendar's height depends on its width; set it before it is drawn (it renders lazily),
// so content below doesn't shift while someone is scrolling or jumping past it
function sizeDaily() {
  const w = $("chDaily").clientWidth || 800;
  const cell = Math.max(5, Math.min(22, Math.floor((w - 44) / 54)));
  $("chDaily").style.height = `${cell * 7 + 56}px`;
  return cell;
}
function renderDaily() {
  const d = state.data, y = +state.dayYear, dd = d.daily, data = [];
  for (let i = 0; i < dd.h.length; i++) {
    const iso = isoOf(addDays(dd.start, i));
    if (+iso.slice(0, 4) === y) data.push([iso, dd.h[i], i]);
  }
  const vmax = Math.max(1, quantile(dd.h, 0.99));
  const cell = sizeDaily();
  chart("chDaily").resize();
  const muted = css("--muted");
  chart("chDaily").setOption({
    ...base(),
    visualMap: { show: false, min: 0, max: vmax, dimension: 1, inRange: { color: ramp() } },
    calendar: { range: String(y), top: 24, left: 30, right: 6, cellSize: [cell, cell],
      itemStyle: { borderColor: css("--surface"), borderWidth: cell > 9 ? 2 : 1, color: css("--surface-2") },
      splitLine: { show: false }, yearLabel: { show: false },
      dayLabel: { firstDay: 1, nameMap: ["S", "M", "T", "W", "T", "F", "S"], color: muted, fontSize: 10 },
      monthLabel: { color: css("--ink-2"), fontSize: cell > 9 ? 11 : 9 } },
    tooltip: { ...base().tooltip, formatter: (p) => {
      const [iso, h, i] = p.data;
      return `<b>${niceDate(iso)}</b><br>Harmonized: <b>${fmt(h, 1)}</b> fire cell-days<br>` +
             `MODIS raw: ${fmt(dd.m[i])} · VIIRS raw: ${dd.v[i] == null ? "n/a" : fmt(dd.v[i])}<br>` +
             `FRP: ${fmt(dd.frp[i])} MW<br><span style="color:${muted}">Source: ${sourceOf(iso)}</span>`; } },
    series: [{ type: "heatmap", coordinateSystem: "calendar", data }],
  }, true);
}

function renderTiming() {
  const ss = state.data.seasons.filter((s) => s.start_off != null);
  const s0 = state.data.season_start_month;
  chart("chTiming").setOption({
    ...base(),
    grid: { left: 64, right: 12, top: 8, bottom: 26 },
    xAxis: { type: "value", min: 0, max: 365, interval: 30.4, ...axisCommon(),
             axisLabel: { color: css("--muted"), fontSize: 11, formatter: (v) => MONTHS[(s0 - 1 + Math.round(v / 30.4)) % 12] } },
    yAxis: { type: "category", data: ss.map((s) => s.label), inverse: true, ...axisCommon(), splitLine: { show: false } },
    tooltip: { ...base().tooltip, trigger: "axis", axisPointer: { type: "shadow", shadowStyle: { color: css("--grid"), opacity: 0.5 } },
      formatter: (ps) => { const s = ss[ps[0].dataIndex];
        return `<b>Season ${s.label}</b><br>Opens ${niceDate(s.start)}<br>Peak ${niceDate(s.peak)}<br>Closes ${niceDate(s.end)}<br>${s.length} days`; } },
    series: [
      { type: "bar", stack: "t", data: ss.map((s) => s.start_off), itemStyle: { color: "transparent" }, barWidth: "58%", silent: true },
      { type: "bar", stack: "t", data: ss.map((s) => s.end_off - s.start_off), itemStyle: { color: css("--q3"), borderRadius: 4 } },
      { type: "scatter", data: ss.map((s) => [s.peak_off, s.label]), symbolSize: 8, itemStyle: { color: css("--ink"), borderColor: css("--surface"), borderWidth: 2 } },
    ],
  }, true);
}

function renderTotals() {
  const ss = state.data.seasons;
  const mean = ss.reduce((a, s) => a + s.total, 0) / Math.max(1, ss.length);
  const [lo2, lo1, , hi1, hi2] = diverging();
  const colorOf = (z) => (z >= 2 ? hi2 : z >= 1 ? hi1 : z <= -2 ? lo2 : z <= -1 ? lo1 : css("--axis"));
  chart("chTotals").setOption({
    ...base(),
    grid: { left: 64, right: 24, top: 24, bottom: 26 },
    xAxis: { type: "value", ...axisCommon(), axisLabel: { color: css("--muted"), fontSize: 11, formatter: kfmt } },
    yAxis: { type: "category", data: ss.map((s) => s.label), inverse: true, ...axisCommon(), splitLine: { show: false } },
    tooltip: { ...base().tooltip, trigger: "axis", axisPointer: { type: "shadow", shadowStyle: { color: css("--grid"), opacity: 0.5 } },
      formatter: (ps) => { const s = ss[ps[0].dataIndex];
        return `<b>Season ${s.label}</b><br>${fmt(s.total)} fire cell-days<br>${mean ? signed(((s.total - mean) / mean) * 100, 0) : "–"}% vs mean · ${signed(s.z)} σ` +
               (s.from_modis ? `<br><span style="color:${css("--muted")}">Harmonized from MODIS</span>` : ""); } },
    series: [{ type: "bar", barWidth: "58%", data: ss.map((s) => ({ value: s.total, itemStyle: { color: colorOf(s.z), borderRadius: [0, 4, 4, 0] } })),
      markLine: { symbol: "none", silent: true, data: [{ xAxis: mean }], lineStyle: { color: css("--ink-2"), type: "dashed" },
                  label: { formatter: "Mean", color: css("--ink-2"), position: "start" } } }],
  }, true);
}

function renderYearly() {
  const y = state.data.yearly;
  const series = [
    { name: "Harmonized (VIIRS-equivalent)", data: y.h, color: css("--s-harm"), width: 4, label: "Harmonized" },
    { name: "Raw VIIRS", data: y.v, color: css("--s-viirs"), width: 2, dash: true, label: null },
    { name: "Raw MODIS", data: y.m, color: css("--s-modis"), width: 2, label: "Raw MODIS" },
  ];
  const vStart = state.data.range.viirs_start.slice(0, 4);
  chart("chYearly").setOption({
    ...base(),
    grid: { left: 56, right: 86, top: 16, bottom: 28 },
    xAxis: { type: "category", data: y.years.map(String), boundaryGap: false, ...axisCommon(), splitLine: { show: false } },
    yAxis: { type: "value", min: 0, ...axisCommon(), axisLine: { show: false }, axisLabel: { color: css("--muted"), fontSize: 11, formatter: kfmt } },
    tooltip: { ...base().tooltip, trigger: "axis", axisPointer: { type: "line", lineStyle: { color: css("--axis") } }, valueFormatter: (v) => (v == null ? "n/a" : fmt(v)) },
    series: series.map((s, i) => ({
      name: s.name, type: "line", data: s.data, symbol: "circle", symbolSize: 6, showSymbol: false,
      lineStyle: { width: s.width, color: s.color, type: s.dash ? "dashed" : "solid" }, itemStyle: { color: s.color, borderColor: css("--surface"), borderWidth: 2 },
      endLabel: { show: !!s.label, formatter: s.label || "", color: css("--ink-2"), fontSize: 11 },
      markArea: i === 0 ? { silent: true, itemStyle: { color: css("--surface-2") }, label: { color: css("--muted"), fontSize: 11, position: "insideTop" },
        data: [[{ name: "VIIRS era", xAxis: vStart }, { xAxis: String(y.years[y.years.length - 1]) }]] } : undefined,
    })),
  }, true);
  $("yearLegend").innerHTML = series.map((s) => `<span class="key"><span class="ln${s.dash ? " dash" : ""}" style="border-color:${s.color}"></span>${s.name}</span>`).join("");
}

function renderScatter() {
  const h = state.data.harmonization, pts = h.scatter;
  const xmax = Math.max(1, ...pts.map((p) => p[0] || 0)), c = css("--s-modis");
  chart("chScatter").setOption({
    ...base(),
    grid: { left: 60, right: 16, top: 30, bottom: 44 },
    xAxis: { type: "value", name: "MODIS cell-days / month", nameLocation: "middle", nameGap: 28, nameTextStyle: { color: css("--muted") }, ...axisCommon(),
             axisLabel: { color: css("--muted"), fontSize: 11, formatter: kfmt } },
    yAxis: { type: "value", name: "VIIRS cell-days / month", nameTextStyle: { color: css("--muted"), align: "left" }, ...axisCommon(), axisLine: { show: false },
             axisLabel: { color: css("--muted"), fontSize: 11, formatter: kfmt } },
    tooltip: { ...base().tooltip, formatter: (p) => p.seriesIndex === 0
      ? `<b>${MONTHS[p.data[2] - 1]}</b><br>MODIS ${fmt(p.data[0])} · VIIRS ${fmt(p.data[1])}<br>ratio ${(p.data[1] / Math.max(1, p.data[0])).toFixed(2)}` : "" },
    series: [
      { type: "scatter", data: pts, symbolSize: 8, itemStyle: { color: c, opacity: 0.75, borderColor: css("--surface"), borderWidth: 1 } },
      { type: "line", data: [[0, 0], [xmax, xmax * h.k_all]], symbol: "none", silent: true, lineStyle: { color: css("--ink-2"), type: "dashed", width: 1.5 } },
    ],
  }, true);
  $("scatLegend").innerHTML = `<span class="key"><span class="sw" style="background:${c};border-radius:50%"></span>One month, 2012–${state.data.range.end.slice(0, 4)}</span>` +
    `<span class="key"><span class="ln dash" style="border-color:${css("--ink-2")}"></span>Fit: VIIRS ≈ ${h.k_all} × MODIS</span>`;
}

function renderCV() {
  const h = state.data.harmonization;
  $("cvTable").innerHTML = `
    <p class="sub">Monthly fit R² <b>${h.r2_monthly ?? "–"}</b> · median error <b>${h.cv_median_ape ?? "–"}%</b> (Terra+Aqua), <b>${h.cv_median_ape_terra ?? "–"}%</b> (Terra-only).
    Terra-only and Terra+Aqua estimates agree within <b>${h.terra_check_ape ?? "–"}%</b> for 2003–2011 (a check independent of VIIRS).
    Worldwide prior: 1 MODIS ≈ ${h.k_world} VIIRS cell-days.</p>
    ${h.low_counts ? `<p class="sub caution"><b>Caution, indicative only:</b> ${confidenceIssues(h).map((i) => i.long).join("; ")}. Annual totals and season timing are more reliable than single months; a larger area with more fires gives a tighter fit.</p>` : ""}
    <table><thead><tr><th>Year</th><th class="num">VIIRS observed</th><th class="num">Predicted</th><th class="num">Error</th><th class="num">Raw MODIS</th></tr></thead><tbody>
    ${h.cv.map((r) => `<tr><td>${r.year}</td><td class="num">${fmt(r.obs)}</td><td class="num">${fmt(r.pred)}</td><td class="num">${r.ape == null ? "–" : r.ape + "%"}</td><td class="num">${fmt(r.raw)}</td></tr>`).join("")}
    </tbody></table>`;
}

const RENDER = { calendar: renderCalendar, profile: renderProfile, daily: renderDaily, timing: renderTiming,
                 totals: renderTotals, yearly: renderYearly, scatter: renderScatter };

// ───────────────────────── export / share / help ─────────────────────────
function downloadCSV() {
  if (!state.data) return;
  const d = state.data.daily;
  const rows = ["date,modis_cell_days,viirs_cell_days,harmonized_cell_days,frp_mw,source"];
  for (let i = 0; i < d.h.length; i++) {
    const iso = isoOf(addDays(d.start, i));
    rows.push([iso, d.m[i], d.v[i] ?? "", d.h[i], d.frp[i], sourceOf(iso)].join(","));
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([rows.join("\n")], { type: "text/csv" }));
  a.download = `firecal_${state.aoi.country || state.aoi.bbox.join("_")}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function share() {
  const url = location.href;
  try {
    if (navigator.share && matchMedia("(pointer: coarse)").matches) await navigator.share({ title: `FireCal: ${$("aoiTitle").textContent}`, url });
    else { await navigator.clipboard.writeText(url); toast("Link copied: anyone can open this exact view"); }
  } catch (_) { toast(url); }
}

function printReport() {
  markAllDirty();
  // render every chart before printing (lazy ones may be off-screen)
  for (const id of Object.keys(renderers)) { try { renderers[id](); } catch (_) {} }
  setTimeout(() => window.print(), 300);
}

function locateMe() {
  if (!navigator.geolocation) { toast("Location isn't available on this device"); return; }
  toast("Finding your location…");
  navigator.geolocation.getCurrentPosition(async (p) => {
    try {
      const r = await FireData.locate(p.coords.longitude, p.coords.latitude);
      if (r.country) selectAOI({ country: r.country });
      else {
        const { longitude: x, latitude: y } = p.coords;
        selectAOI({ bbox: [x - 1, y - 1, x + 1, y + 1].map((v) => Math.round(v * 10) / 10) });
      }
    } catch (e) { toast(`Could not look up your location: ${e.message}`); }
  }, () => toast("Location permission denied"), { timeout: 10000 });
}

// ───────────────────────── install & offline ─────────────────────────
let installPrompt = null;
const isStandalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
function platform() {
  const ua = navigator.userAgent;
  if (/iPad|iPhone|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return "ios";
  if (/Android/.test(ua)) return /SamsungBrowser/.test(ua) ? "samsung" : /Firefox/.test(ua) ? "android-firefox" : "android";
  if (/Firefox/.test(ua)) return "firefox";
  if (/Safari/.test(ua) && !/Chrome|Chromium|Edg|OPR/.test(ua)) return "mac-safari";
  return /Edg/.test(ua) ? "edge" : "desktop";
}
addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installPrompt = e; $("installBtn").classList.add("ready"); });
addEventListener("appinstalled", () => { installPrompt = null; toast("FireCal is installed. It opens from its icon and works offline."); });

function installSteps() {
  if (isStandalone()) return `<p class="ok-line">✓ FireCal is installed on this device and opens from its own icon.</p>`;
  if (installPrompt) return `<p>Install FireCal as an app: its own icon, its own window, and it works offline.</p>
    <p><button type="button" id="doInstall">Install FireCal</button></p>`;
  const steps = {
    ios: ["Tap the <b>Share</b> button <span class=\"kbd-ico\">⬆︎</span> (bottom of the screen on iPhone, top on iPad).",
          "Scroll down and tap <b>Add to Home Screen</b>.", "Tap <b>Add</b>. FireCal now opens full-screen from its icon, also offline."],
    android: ["Open the browser menu <b>⋮</b> (top right).", "Tap <b>Install app</b> (or <b>Add to Home screen</b>).", "Confirm with <b>Install</b>."],
    samsung: ["Tap the menu <b>≡</b> (bottom right).", "Tap <b>Add page to</b> → <b>Home screen</b>.", "Confirm with <b>Add</b>."],
    "android-firefox": ["Open the menu <b>⋮</b>.", "Tap <b>Install</b> (or <b>Add to Home screen</b>).", "Confirm."],
    "mac-safari": ["In the menu bar choose <b>File → Add to Dock</b> (Safari 17 or newer).", "Click <b>Add</b>. FireCal opens from the Dock like any app."],
    edge: ["Click the <b>App available</b> icon in the address bar, or open the menu <b>…</b> → <b>Apps</b> → <b>Install this site as an app</b>.", "Click <b>Install</b>."],
    desktop: ["Click the <b>install</b> icon <span class=\"kbd-ico\">⊕</span> at the right end of the address bar, or open the menu <b>⋮</b> → <b>Cast, save and share</b> → <b>Install page as app</b>.", "Click <b>Install</b>."],
    firefox: ["Firefox on computers doesn't install web apps. Open this page in <b>Chrome</b>, <b>Edge</b> or <b>Safari</b> to install it,", "or keep using it here: saving for offline below works in Firefox too."],
  }[platform()];
  return `<ol class="steps">${steps.map((t) => `<li>${t}</li>`).join("")}</ol>`;
}

function openInstall() {
  $("installSteps").innerHTML = installSteps();
  const btn = $("doInstall");
  if (btn) btn.onclick = async () => {
    installPrompt.prompt();
    const { outcome } = await installPrompt.userChoice.catch(() => ({}));
    if (outcome === "accepted") installPrompt = null;
    $("installSteps").innerHTML = installSteps();
  };
  const local = FireData.mode !== "static";
  $("saveOffline").hidden = local; $("offlineIntro").hidden = local;
  if (local) $("offlineStatus").textContent = "This copy of FireCal runs on this computer, so it already works without internet; only live fires need a connection.";
  else offlineCount().then(renderOfflineStatus);
  openDialog($("installDlg"));
}

async function offlineCount() { // countries whose calendar is saved on this device
  try {
    const keys = await (await caches.open("firecal-data-v2")).keys();
    return new Set(keys.map((r) => decodeURIComponent(new URL(r.url).pathname)).filter((p) => p.includes("/data/countries/"))).size;
  } catch (_) { return null; }
}
function renderOfflineStatus(n) {
  const total = state.meta?.countries.filter((c) => c.ready).length || 0;
  if (n != null) $("offlineStatus").textContent = n >= total && total ? `✓ All ${total} countries are saved on this device.` : `${n} of ${total} countries saved on this device.`;
}

async function saveOffline() {
  if (!("serviceWorker" in navigator) || !navigator.serviceWorker.controller) { toast("Offline saving needs a moment to get ready; reload the page once and try again."); return; }
  if (!navigator.onLine) { toast("You're offline. Connect once to save everything for offline use."); return; }
  const ready = state.meta.countries.filter((c) => c.ready);
  const layers = ["all", ...[...$("mapYear").options].map((o) => o.value).filter((v) => v !== "all").map((y) => `y${y}`),
                  ...Array.from({ length: 12 }, (_, i) => `m${String(i + 1).padStart(2, "0")}`)];
  const urls = [...ready.map((c) => `data/countries/${encodeURIComponent(c.id)}.json`),
                ...layers.flatMap((k) => [`data/map/${k}/overview.json`, `data/map/${k}/index.json`])];
  const bar = $("offlineBar"), btn = $("saveOffline");
  bar.hidden = false; btn.disabled = true;
  const channel = new MessageChannel();
  channel.port1.onmessage = async (e) => {
    const { done, total, failed, finished } = e.data;
    bar.querySelector("i").style.width = `${Math.round((done / total) * 100)}%`;
    $("offlineStatus").textContent = `Saving… ${done} of ${total} files`;
    if (finished) {
      btn.disabled = false; bar.hidden = true;
      renderOfflineStatus(await offlineCount());
      toast(failed ? `Saved for offline, except ${failed} files (connection dropped); try again to finish.` : "Everything is saved: FireCal now works fully offline.");
    }
  };
  navigator.serviceWorker.controller.postMessage({ type: "save-offline", urls }, [channel.port2]);
}

function updateNetwork() {
  const off = !navigator.onLine;
  $("offlineChip").hidden = !off;
  document.documentElement.classList.toggle("is-offline", off);
  if (off) offlineCount().then((n) => { $("offlineChip").title = n != null ? `Offline: showing data saved on this device (${n} countries saved)` : "Offline: showing saved data"; });
}

// pop-up dialogs; browsers without <dialog> (e.g. iOS before 15.4) get a small, integrity-checked polyfill
async function openDialog(d) {
  if (!d.showModal) {
    try {
      FireLoad.style("https://cdnjs.cloudflare.com/ajax/libs/dialog-polyfill/0.5.6/dialog-polyfill.min.css", "sha384-evadC5F6i80z/u8ItaHQAncLnFiYmo9BHd5tO/xNgzKN/RaAM3gKFB9E5sct9Z4f");
      await FireLoad.script("https://cdnjs.cloudflare.com/ajax/libs/dialog-polyfill/0.5.6/dialog-polyfill.min.js", "sha384-+LorgyMYKOvmUpn/wyvKBteKOl4HgVqbkVD00eumg/4kYtpNdCe8ljH+ERe4939i");
      window.dialogPolyfill.registerDialog(d);
    } catch (_) { d.setAttribute("open", ""); return; }
  }
  d.showModal();
}
function openHelp() { openDialog($("help")); }

// ───────────────────────── wiring ─────────────────────────
async function loadMeta() {
  state.meta = await FireData.meta();
  state.byId = Object.fromEntries(state.meta.countries.map((c) => [c.id, c]));
  $("countryList").innerHTML = state.meta.countries.map((c) => `<option value="${esc(c.name)}"></option>`).join("");
  renderReadyCount();
  styleCountries();
}

function renderReadyCount() {
  const nReady = state.meta.countries.filter((c) => c.ready).length;
  $("readyCount").textContent = FireData.mode === "static"
    ? `${nReady} of ${state.meta.countries.length} countries available${state.meta.live?.fetched_at ? ` · live fires checked ${new Date(state.meta.live.fetched_at).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" })}` : ""}`
    : `${nReady} of ${state.meta.countries.length} countries cached · any country loads on demand`;
}

// website: when the browser has read newer live fires straight from NASA, redraw what shows them
FireData.onLiveUpdate?.((live) => {
  if (!state.meta) return;
  state.meta.live = live; renderReadyCount();
  if (state.data && state.aoi) loadNowcast(state.aoi, state.req, state.data);
  if (state.layer === "live") refreshLayer();
});

async function init() {
  const t = store.get("firecal-theme");
  if (t) document.documentElement.dataset.theme = t;
  setupLazy();
  setupNav();
  try {
    await loadMeta();
  } catch (e) {
    setBusy(true, `<b>The FireCal server isn't running.</b>
      <p>Start it from the project folder, then press Retry:</p>
      <code>./start.sh</code>
      <p class="hint">Details: ${esc(e.message)}</p>
      <button type="button" onclick="location.reload()">Retry</button>`);
    return;
  }
  const y1 = +state.meta.range.end.slice(0, 4);
  $("mapYear").innerHTML = `<option value="all">All years (mean)</option>` + Array.from({ length: y1 - 2000 + 1 }, (_, i) => `<option>${y1 - i}</option>`).join("");
  $("mapMonth").innerHTML = `<option value="0">All months</option>` + MONTHS.map((m, i) => `<option value="${i + 1}">${m}</option>`).join("");

  const fromHash = aoiFromHash();
  const saved = (() => { const p = new URLSearchParams(store.get("firecal-aoi") || ""); return p.get("country") && state.byId[p.get("country")] ? { country: p.get("country") } : null; })();
  const firstReady = state.meta.countries.find((c) => c.id === "Nigeria" && c.ready) || state.meta.countries.find((c) => c.ready);
  state.aoi = fromHash || saved || (firstReady ? { country: firstReady.id } : { country: "Nigeria" });

  if (matchMedia("(max-width: 1100px)").matches) $("methodBox").open = false;
  if (window.maplibregl) initMap(); else mapUnavailable();
  history.replaceState(null, "", `#${aoiQuery(state.aoi)}`);
  if (state.aoi.country) $("search").value = state.byId[state.aoi.country].name;
  loadAOI();
  if (hashProblem) toast(hashProblem, 7000);
  if (!store.get("firecal-seen-help")) { store.set("firecal-seen-help", "1"); openHelp(); }

  const byName = (v) => state.meta.countries.find((c) => c.name.toLowerCase() === v.trim().toLowerCase());
  $("search").addEventListener("change", () => { const c = byName($("search").value); if (c) { selectAOI({ country: c.id }); $("search").blur(); } });
  $("search").addEventListener("focus", () => $("search").select());
  $("searchForm").onsubmit = (e) => {
    e.preventDefault();
    const v = $("search").value.trim().toLowerCase();
    if (!v) return;
    const c = byName(v) || state.meta.countries.find((c) => c.name.toLowerCase().startsWith(v)) || state.meta.countries.find((c) => c.name.toLowerCase().includes(v));
    if (c) { selectAOI({ country: c.id }); $("search").blur(); } else toast("No matching country");
  };
  $("coordForm").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target), b = ["w", "s", "e", "n"].map((k) => +f.get(k));
    if (!(b[0] < b[2] && b[1] < b[3])) { toast("West must be less than East, South less than North"); return; }
    selectAOI({ bbox: b });
  };
  $("draw").onclick = () => setDrawing(!drawing);
  $("locate").onclick = locateMe;
  $("layerMode").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.layer = b.dataset.layer;
    $("layerMode").querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    refreshLayer();
  };
  $("audience").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.audience = b.dataset.aud;
    $("audience").querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    renderBriefing();
  };
  $("mapYear").onchange = refreshLayer;
  $("mapMonth").onchange = refreshLayer;
  $("season").onchange = (e) => { state.season = +e.target.value; dirty.add("chProfile"); flush(); };
  $("dayYear").onchange = (e) => { state.dayYear = +e.target.value; dirty.add("chDaily"); flush(); };
  $("calMode").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.calMode = b.dataset.mode;
    $("calMode").querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    dirty.add("chCalendar"); flush();
  };
  $("csv").onclick = downloadCSV;
  $("share").onclick = share;
  $("print").onclick = printReport;
  $("helpBtn").onclick = openHelp;
  $("installBtn").hidden = false; $("installBtn").onclick = openInstall;
  $("saveOffline").onclick = saveOffline;
  addEventListener("online", updateNetwork); addEventListener("offline", updateNetwork); updateNetwork();
  // last resort (no <dialog> and no polyfill): the dialogs' close buttons simply hide them
  for (const f of document.querySelectorAll('dialog form[method="dialog"]'))
    f.addEventListener("submit", (e) => { const d = f.closest("dialog"); if (!d.close) { e.preventDefault(); d.removeAttribute("open"); } });
  $("theme").onclick = () => {
    const next = isDark() ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store.set("firecal-theme", next);
    rethemed();
  };
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rethemed);
  addEventListener("hashchange", () => { const a = aoiFromHash(); if (a) selectAOI(a); else if (hashProblem) toast(hashProblem, 7000); });
  let rt;
  addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { Object.values(charts).forEach((c) => c.resize()); if (state.data) { sizeDaily(); dirty.add("chDaily"); flush(); } }, 150); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && drawing) { setDrawing(false); dragStart = null; drawAOI(); }
    if (e.key === "/" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); $("search").focus(); }
  });

  if ("serviceWorker" in navigator && location.protocol !== "file:") {
    const firstVisit = !navigator.serviceWorker.controller;
    navigator.serviceWorker.register("sw.js").catch(() => {});
    // on a first visit the offline helper starts after this page has loaded its data: hand it what was
    // already loaded (the area you opened, its map layer), so that is saved for airplane mode too
    if (firstVisit) navigator.serviceWorker.addEventListener("controllerchange", () => {
      const urls = FireData.fetchedUrls();
      if (urls.length) navigator.serviceWorker.controller?.postMessage({ type: "save-offline", urls });
    }, { once: true });
  }
}

function rethemed() {
  Object.values(charts).forEach((c) => c.dispose());
  for (const k of Object.keys(charts)) delete charts[k];
  renderAll(); renderNowcast(); refreshBasemap();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
