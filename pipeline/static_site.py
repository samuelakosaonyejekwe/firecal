"""Build the static (GitHub Pages) edition of FireCal from processed country grids.

Everything the server computes on request is precomputed here as plain files:

  site/index.html, static/…            the web app, switched to static mode with relative paths
  site/data/meta.json                  countries, record range, worldwide calibration prior
  site/data/countries/<id>.json        full calendar analysis per country (same JSON as /api/calendar)
  site/data/tiles/<ty>_<tx>.bin.gz     every fire cell-day in a 2° tile at full 0.1° precision, so the
                                       browser can analyse ANY drawn box exactly (no snapping)
  site/data/map/<layer>/…              history map layers: 0.5° world overview + 0.1° detail in 10° tiles
  site/data/own_cells.json             per country, its recorded fire cells that lie outside its border
                                       (coasts, border strips, shapeless territories), so browsers count
                                       live fires exactly like the live updater (app/static/live.js)
  build/cells_country.parquet          (yi, xi, country) for every fire cell, used by the live updater
  build/static_cells.parquet           cells dominated by industrial heat, masked in live counts

Usage:  python pipeline/static_site.py --out site --build build
"""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import pathlib
import shutil
import sys

import duckdb
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.analysis import Store  # noqa: E402
from app.constants import (AQUA_START, BOX_TILE as TILE, CELL, MAP_TILE, MODIS_START, OVERVIEW,  # noqa: E402
                           VIIRS_START, WEB_MAX_BOX_DEG2)

DATA = pathlib.Path(os.environ.get("FIRECAL_DATA", ROOT / "data"))
STATIC = ROOT / "app" / "static"
RES = ROOT / "app" / "resources"
DAY0 = MODIS_START.date()
MEMORY_LIMIT = os.environ.get("FIRECAL_BUILD_MEMORY", "2GB")  # DuckDB cap; larger work spills to disk
BAND_TILES = 5  # box tiles are sorted 5 tile-rows (10° of latitude) at a time, keeping temp files small


def write_json(path: pathlib.Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":")))


def build_app(out: pathlib.Path, version: str):
    """Copy the web app and switch it to static mode with paths relative to the site root."""
    shutil.copytree(STATIC, out / "static", dirs_exist_ok=True)
    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{v}}", version)
    html = html.replace('"/static/', '"static/').replace('"/manifest.webmanifest"', '"manifest.webmanifest"')
    html = html.replace("<head>", '<head>\n  <meta name="firecal-edition" content="static">', 1)  # no inline script (CSP)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "sw.js").write_text((STATIC / "sw.js").read_text(encoding="utf-8").replace("{{v}}", version), encoding="utf-8")
    man = json.loads((STATIC / "manifest.webmanifest").read_text())
    man["start_url"], man["scope"], man["id"] = "./", "./", "./"
    for icon in man["icons"] + man.get("screenshots", []):
        icon["src"] = icon["src"].lstrip("/")
    write_json(out / "manifest.webmanifest", man)
    shutil.copy(RES / "world.geojson", out / "world.geojson")    # map outlines (1:110m)
    shutil.copy(RES / "shapes.geojson", out / "shapes.geojson")  # the server's borders, for boxes and "◎ Me"
    shutil.copytree(RES / "places", out / "places", dirs_exist_ok=True)  # town names for the map card (GeoNames)
    (out / ".nojekyll").write_text("")  # serve files as-is (no Jekyll processing)


def build_calendars(store: Store, out: pathlib.Path):
    for i, cid in enumerate(store.ready, 1):
        store._mem.clear()  # each country is analysed once; don't keep its daily series around
        (out / "data" / "countries").mkdir(parents=True, exist_ok=True)
        (out / "data" / "countries" / f"{cid}.json").write_bytes(store.analyze_bytes({"country": cid}))
        print(f"  calendar {i}/{len(store.ready)} {cid}", flush=True)


def _duck(build: pathlib.Path) -> duckdb.DuckDBPyConnection:
    """DuckDB with a hard memory cap; anything bigger spills to disk, so memory stays flat at world scale."""
    tmp = build / "duckdb_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
    con.execute(f"SET temp_directory = '{tmp.as_posix()}'")
    con.execute("SET preserve_insertion_order = false")
    return con


def _write_tile(tdir, ty, tx, parts, index):
    """parts: per sensor (0 MODIS, 1 VIIRS, 2 Terra-only) arrays of (day, idx), sorted."""
    counts, body = [], []
    for day, idx in parts:
        counts.append(len(day))
        body.append(np.diff(day, prepend=0).astype("<u2").tobytes() + idx.astype("<u2").tobytes())
    header = b"FCT1" + np.array([ty, tx], "<i2").tobytes() + np.array(counts, "<u4").tobytes()
    blob = gzip.compress(header + b"".join(body), compresslevel=9, mtime=0)
    (tdir / f"{ty}_{tx}.bin.gz").write_bytes(blob)
    index[f"{ty}_{tx}"] = len(blob)


def build_box_tiles(store: Store, out: pathlib.Path, build: pathlib.Path):
    """Fire cell-days in 2° tiles (borders merged, so nothing is double-counted).

    Streams sorted 10° latitude bands through DuckDB in record batches and writes each tile as soon
    as it is complete, so memory is bounded by MEMORY_LIMIT plus one tile, and temporary disk use by
    one band, whatever the size of the world."""
    files = "[" + ",".join(f"'{store.path(c).as_posix()}'" for c in store.ready) + "]"
    con = _duck(build)
    index = {}
    tdir = out / "data" / "tiles"
    tdir.mkdir(parents=True, exist_ok=True)
    cur, buf = None, []  # current tile key and its pending batches

    def flush():
        if cur is None:
            return
        cols = {k: np.concatenate([b[k] for b in buf]) for k in ("s", "day", "idx")}
        parts = [(cols["day"][cols["s"] == s].astype(np.int64), cols["idx"][cols["s"] == s]) for s in (0, 1, 2)]
        _write_tile(tdir, cur[0], cur[1], parts, index)

    lo, hi = con.execute(f"SELECT min(yi), max(yi) FROM read_parquet({files})").fetchone()
    band = TILE * BAND_TILES
    for y0 in range((lo // band) * band, hi + 1, band):  # one latitude band at a time keeps each sort small
        reader = con.execute(f"""
            SELECT CAST(floor(yi / {TILE}) AS INTEGER) AS ty, CAST(floor(xi / {TILE}) AS INTEGER) AS tx,
                   CAST(s AS INTEGER) AS s, CAST(CAST(d AS DATE) - DATE '{DAY0}' AS INTEGER) AS day,
                   CAST((yi - floor(yi / {TILE}) * {TILE}) * {TILE} + (xi - floor(xi / {TILE}) * {TILE}) AS INTEGER) AS idx
            FROM (SELECT DISTINCT d, yi, xi, s FROM read_parquet({files}) WHERE yi >= {y0} AND yi < {y0 + band})
            ORDER BY ty, tx, s, day, idx""").to_arrow_reader(1_000_000)
        for batch in reader:
            b = {k: batch.column(k).to_numpy() for k in ("ty", "tx", "s", "day", "idx")}
            change = np.flatnonzero((np.diff(b["ty"]) != 0) | (np.diff(b["tx"]) != 0)) + 1
            for a, z in zip(np.r_[0, change], np.r_[change, len(b["ty"])]):
                key = (int(b["ty"][a]), int(b["tx"][a]))
                if key != cur:
                    flush()
                    cur, buf = key, []
                buf.append({k: b[k][a:z] for k in ("s", "day", "idx")})
    flush()
    write_json(tdir / "index.json", {"tile_cells": TILE, "day0": DAY0.isoformat(), "tiles": index})
    # helpers for the live updater
    build.mkdir(parents=True, exist_ok=True)
    con.execute(f"""COPY (SELECT DISTINCT yi, xi, regexp_extract(filename, '/countries/([^/]+)/', 1) AS cid
                          FROM read_parquet({files}, filename = true)) TO '{(build / "cells_country.parquet").as_posix()}' (FORMAT parquet)""")
    masks = [p.as_posix() for p in store.cdir.glob("*/static_cells.parquet")]
    if masks:
        con.execute(f"COPY (SELECT DISTINCT yi, xi FROM read_parquet({masks})) TO '{(build / 'static_cells.parquet').as_posix()}' (FORMAT parquet)")
    con.close()
    print(f"  {len(index)} box tiles, {sum(index.values()) / 1e6:.1f} MB", flush=True)


def build_own_cells(out: pathlib.Path, build: pathlib.Path):
    """Each country's recorded fire cells whose centre is outside its own border (see the docstring)."""
    import shapely
    from shapely.geometry import shape
    cc = duckdb.execute(f"SELECT yi, xi, cid FROM '{(build / 'cells_country.parquet').as_posix()}' ORDER BY cid, yi, xi").df()
    geoms = {f["properties"]["id"]: shape(f["geometry"])
             for f in json.loads((RES / "shapes.geojson").read_text(encoding="utf-8"))["features"]}
    own = {}
    for cid, g in cc.groupby("cid"):
        pts = shapely.points((g.xi.to_numpy() + 0.5) / CELL, (g.yi.to_numpy() + 0.5) / CELL)
        outside = ~shapely.intersects(geoms[cid], pts) if cid in geoms else np.ones(len(g), bool)
        if outside.any():
            own[cid] = g[outside][["yi", "xi"]].astype(int).values.ravel().tolist()
    write_json(out / "data" / "own_cells.json", own)
    print(f"  own cells outside borders: {sum(map(len, own.values())) // 2} in {len(own)} countries", flush=True)


def build_map_layers(store: Store, out: pathlib.Path, build: pathlib.Path):
    """History layers: all years, each year, each calendar month (mean per year).

    Each country's VIIRS-equivalent fire days per (year, month, cell) go into one DuckDB table (on
    disk if it outgrows MEMORY_LIMIT); every layer is then a single aggregation over it."""
    years = list(range(MODIS_START.year, store.end.year + 1))
    nyears = len(years)
    con = _duck(build)
    con.execute("CREATE TABLE cm (cid VARCHAR, y SMALLINT, m TINYINT, xi SMALLINT, yi SMALLINT, val DOUBLE)")
    for cid in store.ready:
        h = json.loads((out / "data" / "countries" / f"{cid}.json").read_text())["harmonization"]
        k, kt = h["k_all"] or 1.0, h["k_terra_all"] or 1.0
        con.execute(f"""
            INSERT INTO cm SELECT '{cid}', year(d), month(d), xi, yi,
                   sum(CASE WHEN d >= DATE '{VIIRS_START.date()}' THEN (s = 1)::INT * 1.0
                            WHEN d >= DATE '{AQUA_START.date()}' THEN (s = 0)::INT * {k}
                            ELSE (s = 2)::INT * {kt} END) AS val
            FROM '{store.path(cid).as_posix()}' GROUP BY ALL HAVING val > 0""")
    layers = [("all", "true", nyears)] + [(f"y{y}", f"y = {y}", 1) for y in years] + \
             [(f"m{m:02d}", f"m = {m}", nyears) for m in range(1, 13)]
    done = 0
    for key, where, norm in layers:
        # per country sum over the layer's period, then the larger value where borders share a cell
        df = con.execute(f"""
            SELECT xi, yi, max(v) AS val FROM (
                SELECT cid, xi, yi, sum(val) / {norm} AS v FROM cm WHERE {where} GROUP BY cid, xi, yi)
            GROUP BY xi, yi HAVING max(v) >= 0.05""").df()  # below 0.05 a cell rounds to "no fire"
        if df.empty:
            continue
        vmax = round(float(np.quantile(df.val, 0.99)), 2)
        ldir = out / "data" / "map" / key
        ov = df.assign(xi=df.xi // OVERVIEW, yi=df.yi // OVERVIEW).groupby(["xi", "yi"], as_index=False).val.sum()
        ov["val"] /= OVERVIEW * OVERVIEW
        write_json(ldir / "overview.json", {"cell": OVERVIEW / CELL, "max": vmax,
                                            "cells": [[int(a), int(b), round(float(c), 2)] for a, b, c in zip(ov.xi, ov.yi, ov.val) if c >= 0.005]})
        df["tx"], df["ty"] = df.xi // MAP_TILE, df.yi // MAP_TILE
        tiles = []
        for (tx, ty), gt in df.groupby(["tx", "ty"]):
            write_json(ldir / f"{ty}_{tx}.json", {"cell": 1 / CELL, "max": vmax,
                                                  "cells": [[int(a), int(b), round(float(c), 1)] for a, b, c in zip(gt.xi, gt.yi, gt.val)]})
            tiles.append(f"{ty}_{tx}")
        write_json(ldir / "index.json", {"tile_cells": MAP_TILE, "tiles": tiles, "max": vmax})
        done += 1
    con.close()
    print(f"  {done} map layers", flush=True)


def build_meta(store: Store, out: pathlib.Path, version: str):
    ready = set(store.ready)
    pm, pt = store.prior()
    na = DATA / "unavailable.json"  # downloaded from the release by the website workflow
    unavailable = json.loads(na.read_text()) if na.exists() else {}
    write_json(out / "data" / "meta.json", {
        "static": True, "version": version, "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "countries": [{"id": c["id"], "name": c["name"], "view": store.view(c["id"]), "ready": c["id"] in ready,
                       # territories without a border shape are matched by data extent, as on the server
                       **({"extent": store.view(c["id"])} if c["id"] in ready and c["id"] not in store._shape else {}),
                       **({"unavailable": unavailable[c["id"]]} if c["id"] in unavailable and c["id"] not in ready else {})}
                      for c in sorted(store.meta.values(), key=lambda c: c["name"])],
        "range": {"start": MODIS_START.date().isoformat(), "end": store.end.date().isoformat(),
                  "viirs_start": VIIRS_START.date().isoformat()},
        "prior": {"k_world": pm, "k_terra_world": pt}, "max_box_deg2": WEB_MAX_BOX_DEG2, "jobs": {},
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--build", default="build")
    args = ap.parse_args()
    out, build = pathlib.Path(args.out), pathlib.Path(args.build)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    store = Store(DATA, RES)
    if not store.ready:
        sys.exit("no processed countries in data/countries")
    version = hashlib.sha1(b"".join(p.read_bytes() for p in sorted(STATIC.rglob("*")) if p.is_file())).hexdigest()[:10]
    print(f"building static site for {len(store.ready)} countries", flush=True)
    build_app(out, version)
    build_meta(store, out, version)
    build_calendars(store, out)
    build_box_tiles(store, out, build)
    build_own_cells(out, build)
    build_map_layers(store, out, build)
    shutil.rmtree(build / "duckdb_tmp", ignore_errors=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
