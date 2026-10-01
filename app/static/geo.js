/* FireCal geometry for the browser: which countries a box or point touches.
 *
 * Uses the same borders as the server (app/resources/shapes.geojson, Natural Earth 1:50m snapped
 * to a 0.0001° grid) and the same rule (shapely "intersects": touching a border counts). All
 * coordinates are scaled to integers (×10 000) so every test is exact, with no floating-point
 * tolerance. tests/test_geo_parity.py checks this file against shapely on random boxes.
 */
(function (root) {
  "use strict";
  const SCALE = 1e4;
  const q = (v) => Math.round(v * SCALE);

  const orient = (ax, ay, bx, by, cx, cy) => Math.sign((bx - ax) * (cy - ay) - (by - ay) * (cx - ax));
  const between = (a, b, c) => Math.min(a, b) <= c && c <= Math.max(a, b);
  const onSegment = (ax, ay, bx, by, px, py) => orient(ax, ay, bx, by, px, py) === 0 && between(ax, bx, px) && between(ay, by, py);

  function segmentsIntersect(a, b, c, d) { // inclusive: touching or overlapping counts
    const o1 = orient(a[0], a[1], b[0], b[1], c[0], c[1]), o2 = orient(a[0], a[1], b[0], b[1], d[0], d[1]);
    const o3 = orient(c[0], c[1], d[0], d[1], a[0], a[1]), o4 = orient(c[0], c[1], d[0], d[1], b[0], b[1]);
    if (o1 !== o2 && o3 !== o4) return true;
    return (o1 === 0 && onSegment(a[0], a[1], b[0], b[1], c[0], c[1])) || (o2 === 0 && onSegment(a[0], a[1], b[0], b[1], d[0], d[1])) ||
           (o3 === 0 && onSegment(c[0], c[1], d[0], d[1], a[0], a[1])) || (o4 === 0 && onSegment(c[0], c[1], d[0], d[1], b[0], b[1]));
  }

  function prepare(feature) { // integer rings + bbox, computed once per feature
    if (feature._geo) return feature._geo;
    const polys = (feature.geometry.type === "Polygon" ? [feature.geometry.coordinates] : feature.geometry.coordinates)
      .map((p) => p.map((ring) => ring.map(([x, y]) => [q(x), q(y)])));
    let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
    for (const p of polys) for (const [x, y] of p[0]) { w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y); }
    return (feature._geo = { polys, bbox: [w, s, e, n] });
  }

  function ringContains(ring, x, y) { // 1 inside, 0 on the boundary, -1 outside
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i], [xj, yj] = ring[j];
      if (onSegment(xi, yi, xj, yj, x, y)) return 0;
      if ((yi > y) !== (yj > y)) {
        // x of the edge at height y, compared exactly: (x - xi) * (yj - yi) vs (xj - xi) * (y - yi)
        const lhs = (x - xi) * (yj - yi), rhs = (xj - xi) * (y - yi);
        if (yj > yi ? lhs < rhs : lhs > rhs) inside = !inside;
      }
    }
    return inside ? 1 : -1;
  }

  function polyTouchesPoint(p, x, y) { // inside or on the boundary of a polygon with holes
    const outer = ringContains(p[0], x, y);
    if (outer < 0) return false;
    if (outer === 0) return true;
    for (const hole of p.slice(1)) { const h = ringContains(hole, x, y); if (h === 0) return true; if (h > 0) return false; }
    return true;
  }

  function pointTouches(feature, lon, lat) {
    const g = prepare(feature), x = q(lon), y = q(lat), [w, s, e, n] = g.bbox;
    if (x < w || x > e || y < s || y > n) return false;
    return g.polys.some((p) => polyTouchesPoint(p, x, y));
  }

  function boxTouches(feature, bbox) {
    const g = prepare(feature), [bw, bs, be, bn] = bbox.map(q), [w, s, e, n] = g.bbox;
    if (e < bw || w > be || n < bs || s > bn) return false;
    const corners = [[bw, bs], [be, bs], [be, bn], [bw, bn]];
    for (const p of g.polys) {
      if (corners.some(([x, y]) => polyTouchesPoint(p, x, y))) return true;               // box corner inside/on the country
      for (const ring of p) {
        if (ring.some(([x, y]) => x >= bw && x <= be && y >= bs && y <= bn)) return true;  // border point inside/on the box
        for (let i = 1; i < ring.length; i++)
          for (let k = 0; k < 4; k++) if (segmentsIntersect(ring[i - 1], ring[i], corners[k], corners[(k + 1) % 4])) return true;
      }
    }
    return false;
  }

  const api = { boxTouches, pointTouches, SCALE };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.FireGeo = api;
})(typeof self !== "undefined" ? self : this);
