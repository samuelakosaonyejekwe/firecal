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
import time
import urllib.request

import duckdb
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import shape

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.constants import CELL, MAP_TILE  # noqa: E402
from app.feeds import download, newest  # noqa: E402

MAX_OVERVIEW = 15000


def fetch_cells(url: str) -> pd.DataFrame:
    with tempfile.TemporaryDirectory() as tmp:
        csv = pathlib.Path(tmp) / "feed.csv"
        download(url, csv)
        return cells_from_csv(csv)


def cells_from_csv(csv: pathlib.Path) -> pd.DataFrame:
    """NASA's CSV -> fire cell-days (detections and FRP per 0.1° cell and day), nominal/high confidence.
    attrs["latest"] is the newest detection in the file ("YYYY-MM-DD HHMM" UTC), so browsers can tell
    whether a NASA file they read is newer than the published copy (app/static/live.js)."""
    src = f"read_csv('{csv.as_posix()}', types = {{'confidence': 'VARCHAR', 'acq_time': 'VARCHAR', 'acq_date': 'VARCHAR'}})"
    with duckdb.connect() as con:
        g = con.execute(f"""
            SELECT CAST(acq_date AS DATE) AS d, CAST(floor(latitude * {CELL}) AS INTEGER) AS yi,
                   CAST(floor(longitude * {CELL}) AS INTEGER) AS xi, count(*) AS n, sum(frp) AS frp
            FROM {src} WHERE confidence IN ('nominal', 'high', 'n', 'h') GROUP BY ALL""").df()
        g.attrs["latest"] = con.execute(f"SELECT max(acq_date || ' ' || lpad(acq_time, 4, '0')) FROM {src}").fetchone()[0]
    return g


def write(path: pathlib.Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site")
    ap.add_argument("--build", default="build")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep-from", metavar="SITE_URL",
                    help="if NASA can't be reached, keep the live files currently published at this site")
    args = ap.parse_args()
    site, build = pathlib.Path(args.site), pathlib.Path(args.build)
    out = site / "data" / "live"
    kept = build / "live_kept"  # tells the website workflow the published live fires were kept (NASA unreachable)
    kept.unlink(missing_ok=True)

    try:
        # the data already shown: this folder's copy, else the published site's (a fresh site build has none)
        old_meta = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else published_meta(args.keep_from)
        known = ((old_meta["source_bytes"], dt.datetime.fromisoformat(old_meta["source_last_modified"]))
                 if old_meta.get("source_bytes") and old_meta.get("source_last_modified") else None)
        url, first, size = newest(known=known)  # whichever NASA server has the newest data (app/feeds.py)
        cells = None if not args.force and _unchanged(out, first, size) else fetch_cells(url)
    except Exception as e:
        if not args.keep_from:
            raise
        # NASA unreachable: publish everything else with the live fires the website already shows
        print(f"NASA unreachable ({e.__class__.__name__}: {e}); keeping the published live fires", flush=True)
        keep_published(args.keep_from, out, build)
        build.mkdir(parents=True, exist_ok=True)
        kept.write_text(f"{e.__class__.__name__}: {e}\n")
        return
    if cells is None:
        print("NASA feed unchanged; nothing to do")
        sys.exit(3)
    modified = old_meta["source_last_modified"] if old_meta.get("source_bytes") == size else first.isoformat()
    publish(cells, out, build, modified, size)


def _unchanged(out: pathlib.Path, first, size) -> bool:
    old = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else {}
    return old.get("source_bytes") == size or old.get("source_last_modified") == first.isoformat()


def published_meta(site_url: str | None) -> dict:
    """The live meta the website shows now ({} if there is no site to ask, or it can't be read)."""
    if not site_url or not site_url.startswith("http"):
        return {}
    try:
        url = site_url.rstrip("/") + f"/data/live/meta.json?nocache={int(time.time())}"
        return json.load(urllib.request.urlopen(url, timeout=60))
    except Exception:
        return {}


def keep_published(site_url: str, out: pathlib.Path, build: pathlib.Path):
    """Copy the live files the website currently shows (meta, overview, countries, every tile)."""
    def get(path, waits=(5, 15, 30, 60)):  # GitHub Pages can answer 503 for a moment: retry
        for wait in (*waits, None):
            try:
                url = site_url.rstrip("/") + "/data/live/" + path
                if url.startswith("http"):
                    url += f"?nocache={int(time.time())}"  # never a stale CDN copy
                return json.load(urllib.request.urlopen(url, timeout=60))
            except Exception:
                if wait is None:
                    raise
                time.sleep(wait)
    for name in ("meta.json", "overview.json", "countries.json", "tiles/index.json"):
        write(out / name, get(name))
    for t in get("tiles/index.json")["tiles"]:
        write(out / "tiles" / f"{t}.json", get(f"tiles/{t}.json"))
    static = build / "static_cells.parquet"  # the mask comes from the history, not from NASA's live feed
    if static.exists():
        mask = duckdb.execute(f"SELECT yi, xi FROM '{static.as_posix()}' ORDER BY yi, xi").df()
        write(out / "static_cells.json", {"cells": mask[["yi", "xi"]].astype(int).values.ravel().tolist()})


def publish(g: pd.DataFrame, out: pathlib.Path, build: pathlib.Path, modified: str, size: int | None = None):
    """Write the live files from fire cell-days (see the module docstring)."""
    latest = g.attrs.get("latest")  # (pandas doesn't carry attrs through merges)
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
                              "detections": int(g.n.sum()), "static_masked": static.exists(),
                              "latest_detection": latest})
    print(f"live: {len(g)} cell-days, {len(tiles)} tiles, {len(per)} countries, NASA updated {modified}")


if __name__ == "__main__":
    main()
