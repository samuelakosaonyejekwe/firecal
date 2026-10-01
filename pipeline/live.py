"""Refresh the live (near-real-time) files of the static site from NASA FIRMS.

Writes site/data/live/:
  meta.json            when NASA last updated the feed, when we fetched it, the days covered
  overview.json        world map layer (≤15,000 cells)
  tiles/<ty>_<tx>.json 10° tiles of every live fire cell-day (0.1°), for the map and drawn boxes
  countries.json       fire cell-days per complete day for every country (early warning)
  static_cells.json    industrial-heat cells [yi, xi, yi, xi, …], so browsers can apply the same mask
                       when they read NASA's file directly (app/static/live.js)

Cells that are historically dominated by industrial heat (gas flares, plants) are excluded.
Exit code 3 means NASA hasn't published anything new since the last run (nothing to deploy).

Usage:  python pipeline/live.py --site site --build build [--force]
"""
import argparse
import datetime as dt
import json
import pathlib
import sys
import tempfile
import urllib.request

import duckdb
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import shape

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.constants import CELL, MAP_TILE  # noqa: E402
from app.feeds import newest  # noqa: E402

MAX_OVERVIEW = 15000


def fetch_cells(url: str) -> pd.DataFrame:
    with tempfile.TemporaryDirectory() as tmp:
        csv = pathlib.Path(tmp) / "feed.csv"
        urllib.request.urlretrieve(url, csv)
        return cells_from_csv(csv)


def cells_from_csv(csv: pathlib.Path) -> pd.DataFrame:
    """NASA's CSV -> fire cell-days (detections and FRP per 0.1° cell and day), nominal/high confidence."""
    with duckdb.connect() as con:
        return con.execute(f"""
            SELECT CAST(acq_date AS DATE) AS d, CAST(floor(latitude * {CELL}) AS INTEGER) AS yi,
                   CAST(floor(longitude * {CELL}) AS INTEGER) AS xi, count(*) AS n, sum(frp) AS frp
            FROM read_csv('{csv.as_posix()}', types = {{'confidence': 'VARCHAR'}})
            WHERE confidence IN ('nominal', 'high', 'n', 'h') GROUP BY ALL""").df()


def write(path: pathlib.Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site")
    ap.add_argument("--build", default="build")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    site, build = pathlib.Path(args.site), pathlib.Path(args.build)
    out = site / "data" / "live"

    url, first, size = newest()  # whichever NASA server has the newest data (app/feeds.py)
    modified = first.isoformat()
    old = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else {}
    if not args.force and old.get("source_last_modified") == modified:
        print(f"NASA feed unchanged since {modified}; nothing to do")
        sys.exit(3)

    publish(fetch_cells(url), out, build, modified, size)


def publish(g: pd.DataFrame, out: pathlib.Path, build: pathlib.Path, modified: str, size: int | None = None):
    """Write the live files from fire cell-days (see the module docstring)."""
    g["d"] = pd.to_datetime(g["d"])
    static = build / "static_cells.parquet"
    if static.exists():  # drop gas flares / industrial heat (FIRMS NRT has no 'type' column)
        mask = duckdb.execute(f"SELECT yi, xi FROM '{static.as_posix()}' ORDER BY yi, xi").df()
        write(out / "static_cells.json", {"cells": mask[["yi", "xi"]].astype(int).values.ravel().tolist()})  # for live.js
        g = g.merge(mask.assign(_static=1), on=["yi", "xi"], how="left")
        g = g[g._static.isna()].drop(columns="_static")
    days = sorted(g.d.unique())
    iso = [pd.Timestamp(d).date().isoformat() for d in days]
    complete = iso[1:-1]  # rolling 168 h window: first and last calendar days are partial
    g["di"] = g.d.map({d: i for i, d in enumerate(days)})

    # world overview (sum of detections per cell, coarsened)
    cells = g.groupby(["xi", "yi"], as_index=False).n.sum()
    f, ov = 1, cells.rename(columns={"n": "val"})
    while len(ov) > MAX_OVERVIEW:
        f += 1
        ov = cells.assign(xi=cells.xi // f, yi=cells.yi // f).groupby(["xi", "yi"], as_index=False).n.sum().rename(columns={"n": "val"})
    write(out / "overview.json", {"cell": f / CELL, "max": round(float(np.quantile(ov.val, 0.99)), 2) if len(ov) else 0,
                                  "days": iso, "cells": ov[["xi", "yi", "val"]].astype(int).values.tolist()})

    # 10° tiles at full precision: [day index, xi, yi, detections]
    g["tx"], g["ty"] = g.xi // MAP_TILE, g.yi // MAP_TILE
    tiles = {}
    for (tx, ty), t in g.groupby(["tx", "ty"]):
        write(out / "tiles" / f"{ty}_{tx}.json", {"days": iso, "rows": t[["di", "xi", "yi", "n"]].astype(int).values.tolist()})
        tiles[f"{ty}_{tx}"] = len(t)
    write(out / "tiles" / "index.json", {"tile_cells": MAP_TILE, "tiles": tiles})

    # per-country early-warning counts: cells inside the border (Natural Earth) or in the country's own record
    feats = json.loads((ROOT / "app" / "resources" / "shapes.geojson").read_text(encoding="utf-8"))["features"]
    ids = [ft["properties"]["id"] for ft in feats]
    tree = shapely.STRtree([shape(ft["geometry"]) for ft in feats])
    uc = g[["yi", "xi"]].drop_duplicates()
    pts = shapely.points((uc.xi.to_numpy() + 0.5) / CELL, (uc.yi.to_numpy() + 0.5) / CELL)
    pi, gi = tree.query(pts, predicate="intersects")
    link = pd.DataFrame({"yi": uc.yi.to_numpy()[pi], "xi": uc.xi.to_numpy()[pi], "cid": np.array(ids)[gi]})
    own = build / "cells_country.parquet"
    if own.exists():
        own_df = duckdb.execute(f"SELECT yi, xi, cid FROM '{own.as_posix()}'").df()
        link = pd.concat([link, uc.merge(own_df, on=["yi", "xi"])]).drop_duplicates()
    gc = g[g.d.isin(pd.to_datetime(complete))].merge(link, on=["yi", "xi"])
    per = gc.groupby(["cid", "d"]).size().unstack(fill_value=0).reindex(columns=pd.to_datetime(complete), fill_value=0)
    frp = gc.groupby("cid").frp.sum()
    write(out / "countries.json", {"days": complete, "countries": {
        cid: {"cells": [int(v) for v in row], "frp": round(float(frp.get(cid, 0)), 0)} for cid, row in per.iterrows()}})

    write(out / "meta.json", {"source_last_modified": modified, "source_bytes": size, "days": iso, "complete_days": complete,
                              "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                              "detections": int(g.n.sum()), "static_masked": static.exists()})
    print(f"live: {len(g)} cell-days, {len(tiles)} tiles, {len(per)} countries, NASA updated {modified}")


if __name__ == "__main__":
    main()
