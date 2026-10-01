"""app/static/live.js (browsers reading NASA directly) must publish what pipeline/live.py publishes.

Both read the same synthetic NASA 7-day file (mixed confidence, negative coordinates, cells on the
industrial-heat mask, partial edge days) and the results are compared file by file. Skips without Node.
"""
import json
import pathlib
import random
import shutil
import subprocess

import duckdb
import pandas as pd
import pytest

import pipeline.live as live
from app.constants import MAP_TILE

ROOT = pathlib.Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

RUNNER = """
const L = require(process.argv[1]), G = require(process.argv[2]);
const fs = require('fs');
const inp = JSON.parse(fs.readFileSync(0, 'utf8'));
const shapes = JSON.parse(fs.readFileSync(process.argv[3], 'utf8')).features;
const cd = L.parse(fs.readFileSync(inp.csv, 'utf8'), inp.staticCells);
const lv = L.build(cd, inp.tileCells, inp.modified);
const countries = {};
for (const id of inp.countries) countries[id] = L.countryCells(lv, shapes.find((f) => f.properties.id === id), null, G);
process.stdout.write(JSON.stringify({ meta: lv.meta, overview: lv.overview, index: lv.index, tiles: lv.tiles, countries }));
"""

HEADER = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight"
SPOTS = [(9.1, 7.4), (-15.2, 28.3), (-3.4, -60.1), (61.0, 105.0), (35.0, 33.2), (-25.7, 28.2), (52.1, 5.3), (-33.9, 151.1)]


def feed(path: pathlib.Path, rows=6000, seed=11):
    rng = random.Random(seed)
    days = pd.date_range("2026-09-24", "2026-10-01").strftime("%Y-%m-%d")
    lines = [HEADER]
    for _ in range(rows):
        lat0, lon0 = rng.choice(SPOTS)
        lat, lon = round(lat0 + rng.gauss(0, 1.5), 5), round(lon0 + rng.gauss(0, 1.5), 5)
        conf = rng.choice(["n", "h", "l", "nominal", "high", "low"])
        lines.append(f"{lat},{lon},330.1,0.4,0.4,{rng.choice(days)},1230,N,VIIRS,{conf},2.0NRT,290.2,{rng.uniform(0, 40):.2f},D")
    path.write_text("\n".join(lines) + "\n")


def test_browser_live_data_matches_the_published_files(tmp_path):
    csv, build, out = tmp_path / "feed.csv", tmp_path / "build", tmp_path / "site" / "data" / "live"
    feed(csv)
    build.mkdir()
    g = live.cells_from_csv(csv)
    masked = g.drop_duplicates(["yi", "xi"]).sample(25, random_state=1)[["yi", "xi"]]  # pretend these are gas flares
    duckdb.from_df(masked.astype("int16")).write_parquet(str(build / "static_cells.parquet"))
    modified = "2026-10-01T20:54:08+00:00"
    live.publish(g, out, build, modified)

    shapes = ROOT / "app" / "resources" / "shapes.geojson"
    wanted = ["Nigeria", "Zambia", "Brazil", "Russian_Federation", "Cyprus", "South_Africa", "Netherlands", "Australia"]
    inp = {"csv": str(csv), "staticCells": json.loads((out / "static_cells.json").read_text())["cells"],
           "tileCells": MAP_TILE, "modified": modified, "countries": wanted}
    res = subprocess.run(["node", "-e", RUNNER, str(ROOT / "app" / "static" / "live.js"), str(ROOT / "app" / "static" / "geo.js"),
                          str(shapes)], input=json.dumps(inp), capture_output=True, text=True, check=True)
    js = json.loads(res.stdout)
    read = lambda name: json.loads((out / name).read_text())  # noqa: E731

    meta = read("meta.json")
    assert js["meta"]["days"] == meta["days"] and js["meta"]["complete_days"] == meta["complete_days"]
    assert js["meta"]["detections"] == meta["detections"] and js["meta"]["source_last_modified"] == modified

    ov = read("overview.json")
    assert js["overview"]["cell"] == ov["cell"] and js["overview"]["max"] == ov["max"]
    assert sorted(map(tuple, js["overview"]["cells"])) == sorted(map(tuple, ov["cells"]))

    index = read("tiles/index.json")
    assert js["index"] == index
    for k in index["tiles"]:
        t = read(f"tiles/{k}.json")
        assert t["days"] == js["tiles"][k]["days"]
        assert sorted(map(tuple, t["rows"])) == sorted(map(tuple, js["tiles"][k]["rows"]))

    countries = read("countries.json")
    assert countries["days"] == meta["complete_days"]
    hit = 0
    for cid in wanted:
        want = countries["countries"].get(cid, {"cells": [0] * len(meta["complete_days"])})["cells"]
        assert js["countries"][cid] == want, cid
        hit += sum(want) > 0
    assert hit >= 6  # the feed really does put fires in these countries
