"""app/static/geo.js must list exactly the countries the server lists (shapely, same border file)."""
import json
import pathlib
import random
import shutil
import subprocess

import pytest
import shapely
from shapely.geometry import box, shape

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHAPES = ROOT / "app" / "resources" / "shapes.geojson"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

RUNNER = """
const G = require(process.argv[1]);
const fc = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
const inp = JSON.parse(require('fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify({
  boxes: inp.boxes.map((b) => fc.features.filter((f) => G.boxTouches(f, b)).map((f) => f.properties.id).sort()),
  points: inp.points.map(([x, y]) => fc.features.filter((f) => G.pointTouches(f, x, y)).map((f) => f.properties.id).sort()),
}));
"""


def test_geo_js_matches_shapely():
    rng = random.Random(7)
    boxes = []
    for _ in range(400):  # boxes snapped to the 0.1° grid like the app, plus some arbitrary ones
        w, s = rng.uniform(-180, 170), rng.uniform(-60, 75)
        b = [w, s, min(180, w + rng.uniform(0.2, 12)), min(85, s + rng.uniform(0.2, 12))]
        boxes.append([round(v, 1) for v in b] if rng.random() < 0.7 else b)
    points = [[rng.uniform(-180, 180), rng.uniform(-60, 75)] for _ in range(400)]
    feats = json.loads(SHAPES.read_text(encoding="utf-8"))["features"]
    ids = [f["properties"]["id"] for f in feats]
    tree = shapely.STRtree([shape(f["geometry"]) for f in feats])
    want_boxes = [sorted(ids[i] for i in tree.query(box(*b), predicate="intersects")) for b in boxes]
    want_points = [sorted(ids[i] for i in tree.query(shapely.Point(x, y), predicate="intersects")) for x, y in points]
    out = subprocess.run(["node", "-e", RUNNER, str(ROOT / "app" / "static" / "geo.js"), str(SHAPES)],
                         input=json.dumps({"boxes": boxes, "points": points}), capture_output=True, text=True, check=True)
    got = json.loads(out.stdout)
    bad_b = [(b, w, g) for b, w, g in zip(boxes, want_boxes, got["boxes"]) if w != g]
    bad_p = [(p, w, g) for p, w, g in zip(points, want_points, got["points"]) if w != g]
    assert not bad_b, bad_b[:3]
    assert not bad_p, bad_p[:3]
    assert sum(map(len, want_boxes)) > 100  # the boxes really do hit countries
