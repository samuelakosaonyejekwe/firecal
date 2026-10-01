"""Build the static (GitHub Pages) edition of FireCal from processed country grids.

Everything the server computes on request is precomputed here as plain files:

  site/index.html, static/…            the web app, switched to static mode with relative paths
  site/data/meta.json                  countries, record range, worldwide calibration prior
  site/data/countries/<id>.json        full calendar analysis per country (same JSON as /api/calendar)
  site/data/tiles/<ty>_<tx>.bin.gz     every fire cell-day in a 2° tile at full 0.1° precision, so the
                                       browser can analyse ANY drawn box exactly (no snapping)
  site/data/map/<layer>/…              history map layers: 0.5° world overview + 0.1° detail in 10° tiles
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
from app.analysis import CELL, MODIS_START, Store  # noqa: E402

DATA = pathlib.Path(os.environ.get("FIRECAL_DATA", ROOT / "data"))
STATIC = ROOT / "app" / "static"
RES = ROOT / "app" / "resources"
TILE = 20          # box tiles: 2° = 20 cells of 0.1°
MAP_TILE = 100     # map detail tiles: 10° = 100 cells
OVERVIEW = 5       # map overview: 0.5° = 5 cells
DAY0 = MODIS_START.date()


def write_json(path: pathlib.Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":")))


def build_app(out: pathlib.Path, version: str):
    """Copy the web app and switch it to static mode with paths relative to the site root."""
    shutil.copytree(STATIC, out / "static", dirs_exist_ok=True)
    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{v}}", version)
    html = html.replace('"/static/', '"static/').replace('"/manifest.webmanifest"', '"manifest.webmanifest"')
    html = html.replace("<head>", '<head>\n  <script>window.FIRECAL_STATIC = true;</script>', 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "sw.js").write_text((STATIC / "sw.js").read_text(encoding="utf-8").replace("{{v}}", version), encoding="utf-8")
    man = json.loads((STATIC / "manifest.webmanifest").read_text())
    man["start_url"], man["scope"] = "./", "./"
    for icon in man["icons"]:
        icon["src"] = icon["src"].lstrip("/")
    write_json(out / "manifest.webmanifest", man)
    shutil.copy(RES / "world.geojson", out / "world.geojson")
    (out / ".nojekyll").write_text("")  # serve files as-is (no Jekyll processing)


def build_calendars(store: Store, out: pathlib.Path):
    for i, cid in enumerate(store.ready, 1):
        (out / "data" / "countries").mkdir(parents=True, exist_ok=True)
        (out / "data" / "countries" / f"{cid}.json").write_bytes(store.analyze_bytes({"country": cid}))
        print(f"  calendar {i}/{len(store.ready)} {cid}", flush=True)


def build_box_tiles(store: Store, out: pathlib.Path, build: pathlib.Path):
    """Fire cell-days in 2° tiles (borders merged, so nothing is double-counted). One sorted pass."""
    files = "[" + ",".join(f"'{store.path(c).as_posix()}'" for c in store.ready) + "]"
    con = duckdb.connect()
    r = con.execute(f"""
        SELECT CAST(floor(yi / {TILE}) AS INTEGER) AS ty, CAST(floor(xi / {TILE}) AS INTEGER) AS tx, s,
               CAST(d - DATE '{DAY0}' AS INTEGER) AS day,
               CAST((yi - floor(yi / {TILE}) * {TILE}) * {TILE} + (xi - floor(xi / {TILE}) * {TILE}) AS INTEGER) AS idx
        FROM (SELECT DISTINCT d, yi, xi, s FROM read_parquet({files}))
        ORDER BY ty, tx, s, day, idx""").fetchnumpy()
    ty, tx, s, day, idx = (r[k] for k in ("ty", "tx", "s", "day", "idx"))
    index = {}
    tdir = out / "data" / "tiles"
    tdir.mkdir(parents=True, exist_ok=True)
    tile_change = np.flatnonzero((np.diff(ty) != 0) | (np.diff(tx) != 0)) + 1
    for a, b in zip(np.r_[0, tile_change], np.r_[tile_change, len(ty)]):
        counts, parts = [], []
        for sensor in (0, 1, 2):
            sel = slice(a, b)
            m = s[sel] == sensor
            d, i = day[sel][m].astype(np.int64), idx[sel][m]
            counts.append(len(d))
            parts.append(np.diff(d, prepend=0).astype("<u2").tobytes() + i.astype("<u2").tobytes())
        header = b"FCT1" + np.array([ty[a], tx[a]], "<i2").tobytes() + np.array(counts, "<u4").tobytes()
        blob = gzip.compress(header + b"".join(parts), compresslevel=9, mtime=0)
        (tdir / f"{ty[a]}_{tx[a]}.bin.gz").write_bytes(blob)
        index[f"{ty[a]}_{tx[a]}"] = len(blob)
    write_json(tdir / "index.json", {"tile_deg": TILE / CELL, "day0": DAY0.isoformat(), "tiles": index})
    # helpers for the live updater
    build.mkdir(parents=True, exist_ok=True)
    con.execute(f"""COPY (SELECT DISTINCT yi, xi, regexp_extract(filename, '/countries/([^/]+)/', 1) AS cid
                          FROM read_parquet({files}, filename = true)) TO '{(build / "cells_country.parquet").as_posix()}' (FORMAT parquet)""")
    masks = [p.as_posix() for p in store.cdir.glob("*/static_cells.parquet")]
    if masks:
        con.execute(f"COPY (SELECT DISTINCT yi, xi FROM read_parquet({masks})) TO '{(build / 'static_cells.parquet').as_posix()}' (FORMAT parquet)")
    print(f"  {len(index)} box tiles, {sum(index.values()) / 1e6:.1f} MB", flush=True)


def build_map_layers(store: Store, out: pathlib.Path):
    """History layers: all years, each year, each calendar month (mean per year). One query per country."""
    import pandas as pd
    years = list(range(MODIS_START.year, store.end.year + 1))
    nyears = len(years)
    parts: dict[str, list] = {}
    for cid in store.ready:
        h = json.loads((out / "data" / "countries" / f"{cid}.json").read_text())["harmonization"]
        k, kt = h["k_all"] or 1.0, h["k_terra_all"] or 1.0
        g = duckdb.execute(f"""
            SELECT year(d) AS y, month(d) AS m, xi, yi,
                   sum(CASE WHEN d >= DATE '2012-01-20' THEN (s = 1)::INT * 1.0
                            WHEN d >= DATE '2002-07-04' THEN (s = 0)::INT * {k}
                            ELSE (s = 2)::INT * {kt} END) AS val
            FROM '{store.path(cid).as_posix()}' GROUP BY ALL HAVING val > 0""").df()
        if g.empty:
            continue
        parts.setdefault("all", []).append(g.groupby(["xi", "yi"], as_index=False).val.sum().assign(val=lambda x: x.val / nyears))
        for y, gy in g.groupby("y"):
            parts.setdefault(f"y{y}", []).append(gy.groupby(["xi", "yi"], as_index=False).val.sum())
        for m, gm in g.groupby("m"):
            parts.setdefault(f"m{m:02d}", []).append(gm.groupby(["xi", "yi"], as_index=False).val.sum().assign(val=lambda x: x.val / nyears))
    for key, frames in parts.items():
        df = pd.concat(frames).groupby(["xi", "yi"], as_index=False).val.max()  # border cells: keep the larger value
        df = df[df.val >= 0.05]  # keeps files small: below this a cell rounds to "no fire" on the map
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
        write_json(ldir / "index.json", {"tiles": tiles, "max": vmax})
    print(f"  {len(parts)} map layers", flush=True)


def build_meta(store: Store, out: pathlib.Path, version: str):
    ready = set(store.ready)
    pm, pt = store.prior()
    write_json(out / "data" / "meta.json", {
        "static": True, "version": version, "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "countries": [{"id": c["id"], "name": c["name"], "view": store.view(c["id"]), "ready": c["id"] in ready}
                      for c in sorted(store.meta.values(), key=lambda c: c["name"])],
        "range": {"start": MODIS_START.date().isoformat(), "end": store.end.date().isoformat(), "viirs_start": "2012-01-20"},
        "prior": {"k_world": pm, "k_terra_world": pt}, "jobs": {},
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
    build_map_layers(store, out)
    print("done", flush=True)


if __name__ == "__main__":
    main()
