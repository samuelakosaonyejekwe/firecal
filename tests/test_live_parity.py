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
import pipeline.static_site as ss
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
for (const id of inp.countries) countries[id] = L.countryCells(lv, shapes.find((f) => f.properties.id === id), inp.own[id], G);
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
    # each country's recorded cells: everything near it in this feed, so coasts and border strips
    # outside the Natural Earth border count too (as on the server), plus a shapeless territory
    cells = g.drop_duplicates(["yi", "xi"])[["yi", "xi"]]
    near = lambda lat, lon: cells[((cells.yi / 10 - lat).abs() < 2.5) & ((cells.xi / 10 - lon).abs() < 2.5)]  # noqa: E731
    own = pd.concat([near(9.1, 7.4).assign(cid="Nigeria"), near(35.0, 33.2).assign(cid="Cyprus"),
                     near(52.1, 5.3).assign(cid="Netherlands"), near(-15.2, 28.3).assign(cid="Guadeloupe")])
    duckdb.from_df(own).write_parquet(str(build / "cells_country.parquet"))
    ss.build_own_cells(tmp_path / "site", build)
    modified = "2026-10-01T20:54:08+00:00"
    live.publish(g, out, build, modified)

    shapes = ROOT / "app" / "resources" / "shapes.geojson"
    wanted = ["Nigeria", "Zambia", "Brazil", "Russian_Federation", "Cyprus", "South_Africa", "Netherlands", "Australia", "Guadeloupe"]
    own_cells = json.loads((tmp_path / "site" / "data" / "own_cells.json").read_text())
    assert own_cells["Cyprus"] and own_cells["Guadeloupe"]  # the test really exercises cells outside borders
    inp = {"csv": str(csv), "own": own_cells, "staticCells": json.loads((out / "static_cells.json").read_text())["cells"],
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


def test_nasa_unreachable_keeps_the_published_live_files(tmp_path, monkeypatch):
    csv, build = tmp_path / "feed.csv", tmp_path / "build"
    feed(csv, rows=800)
    build.mkdir()
    published = tmp_path / "published"
    live.publish(live.cells_from_csv(csv), published / "data" / "live", build, "2026-10-01T21:54:29+00:00", 123)

    def down(*a, **k):
        raise OSError("no NASA FIRMS server answered")
    monkeypatch.setattr(live, "newest", down)
    monkeypatch.setattr("sys.argv", ["live.py", "--site", str(tmp_path / "site"), "--build", str(build), "--force",
                                     "--keep-from", published.as_uri()])
    live.main()
    got, want = tmp_path / "site" / "data" / "live", published / "data" / "live"
    names = sorted(p.relative_to(want).as_posix() for p in want.rglob("*.json") if p.name != "static_cells.json")
    assert names and all(json.loads((got / n).read_text()) == json.loads((want / n).read_text()) for n in names)
    # the website workflow is told, so it doesn't remember this state as published (it retries when NASA is back)
    assert "no NASA FIRMS server answered" in (build / "live_kept").read_text()


MERGE_RUNNER = """
const L = require(process.argv[1]), G = require(process.argv[2]);
const fs = require('fs');
const inp = JSON.parse(fs.readFileSync(0, 'utf8'));
const shapes = JSON.parse(fs.readFileSync(process.argv[3], 'utf8')).features;
const load = (dir) => {
  const ix = JSON.parse(fs.readFileSync(dir + '/tiles/index.json', 'utf8')), tiles = {};
  for (const k of Object.keys(ix.tiles)) tiles[k] = JSON.parse(fs.readFileSync(dir + '/tiles/' + k + '.json', 'utf8'));
  return L.fromTiles(tiles);
};
const fresh = L.parse(fs.readFileSync(inp.csv24, 'utf8'), inp.staticCells);
const cd = L.merge(load(inp.siteDir), fresh);
const old = L.merge(load(inp.oldSiteDir), fresh);
const lv = L.build(cd, inp.tileCells, 'x');
const countries = {};
for (const id of inp.countries) countries[id] = L.countryCells(lv, shapes.find((f) => f.properties.id === id), null, G);
process.stdout.write(JSON.stringify({ meta: lv.meta, index: lv.index, tiles: lv.tiles, overview: lv.overview, countries, tooOld: old === null }));
"""


def timed_feed(path, rows, until=None, since=None):
    lines = [HEADER]
    for t, lat, lon, conf in rows:
        if (until is None or t <= until) and (since is None or t > since):
            lines.append(f"{lat},{lon},330.1,0.4,0.4,{t:%Y-%m-%d},{t:%H%M},N,VIIRS,{conf},2.0NRT,290.2,3.5,D")
    path.write_text("\n".join(lines) + "\n")


def test_last_24_hours_joined_to_an_older_copy_equals_the_full_feed(tmp_path):
    rng = random.Random(5)
    now = pd.Timestamp("2026-10-01 18:00")
    rows = []
    for _ in range(8000):  # a rolling 7-day window of detections, minute resolution
        t = now - pd.Timedelta(minutes=rng.randrange(0, 7 * 24 * 60))
        lat0, lon0 = rng.choice(SPOTS)
        rows.append((t, round(lat0 + rng.gauss(0, 1.5), 5), round(lon0 + rng.gauss(0, 1.5), 5), rng.choice(["n", "h", "l"])))
    build = tmp_path / "build"
    build.mkdir()
    full, site_csv, csv24, old_csv = (tmp_path / f for f in ("full.csv", "site.csv", "24h.csv", "old.csv"))
    timed_feed(full, rows)                                            # NASA's 7-day file now
    timed_feed(site_csv, rows, until=now - pd.Timedelta(hours=12))    # what GitHub published 12 hours ago
    timed_feed(csv24, rows, since=now - pd.Timedelta(hours=24))       # NASA's last-24-hours file now
    timed_feed(old_csv, rows, until=now - pd.Timedelta(hours=36))     # a copy published 36 hours ago
    want, site = tmp_path / "want", tmp_path / "site"
    live.publish(live.cells_from_csv(full), want, build, "now")
    live.publish(live.cells_from_csv(site_csv), site, build, "then")
    shapes = ROOT / "app" / "resources" / "shapes.geojson"
    wanted = ["Nigeria", "Zambia", "Brazil", "Russian_Federation", "Cyprus", "South_Africa", "Netherlands", "Australia"]
    old_site = tmp_path / "old_site"
    live.publish(live.cells_from_csv(old_csv), old_site, build, "older")
    inp = {"siteDir": str(site), "oldSiteDir": str(old_site), "csv24": str(csv24), "staticCells": [],
           "tileCells": MAP_TILE, "countries": wanted}
    res = subprocess.run(["node", "-e", MERGE_RUNNER, str(ROOT / "app" / "static" / "live.js"), str(ROOT / "app" / "static" / "geo.js"),
                          str(shapes)], input=json.dumps(inp), capture_output=True, text=True, check=True)
    js = json.loads(res.stdout)
    assert js["tooOld"]  # a copy with nothing after the 24-hour file's first day can't be joined (7-day file instead)
    read = lambda name: json.loads((want / name).read_text())  # noqa: E731
    meta = read("meta.json")
    assert js["meta"]["days"] == meta["days"] and js["meta"]["complete_days"] == meta["complete_days"]
    assert js["meta"]["latest_detection"] == meta["latest_detection"] and js["meta"]["detections"] == meta["detections"]
    assert js["index"] == read("tiles/index.json")
    for k in js["index"]["tiles"]:
        assert sorted(map(tuple, read(f"tiles/{k}.json")["rows"])) == sorted(map(tuple, js["tiles"][k]["rows"]))
    ov = read("overview.json")
    assert js["overview"]["max"] == ov["max"] and sorted(map(tuple, js["overview"]["cells"])) == sorted(map(tuple, ov["cells"]))
    countries = read("countries.json")["countries"]
    for cid in wanted:
        assert js["countries"][cid] == countries.get(cid, {"cells": [0] * len(meta["complete_days"])})["cells"], cid
