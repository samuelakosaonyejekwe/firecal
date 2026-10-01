"""Compute the worldwide MODIS->VIIRS calibration prior and save it to app/resources/prior.json.

The prior is the pooled VIIRS / MODIS (and VIIRS / Terra-only) fire cell-day ratio over the
2012+ overlap years across all processed countries. Small or quiet areas lean on it, so it is
kept fixed and versioned in the repository: every machine and the website use the same value,
and an area's results don't change when unrelated countries are loaded. Re-run it deliberately
(e.g. after the world preload) and commit the new file; all results shift slightly when it changes.

Usage:  python pipeline/prior.py
"""
import datetime as dt
import json
import os
import pathlib
import sys

import duckdb
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.constants import PRIOR_FILE, TERRA_DRIFT, VIIRS_START  # noqa: E402

DATA = pathlib.Path(os.environ.get("FIRECAL_DATA", ROOT / "data"))


def compute(files: list[pathlib.Path]) -> dict:
    """Pooled VIIRS/MODIS and VIIRS/Terra-only fire cell-day ratios over the 2012+ overlap."""
    lst = "[" + ",".join(f"'{f.as_posix()}'" for f in files) + "]"
    ov0 = (VIIRS_START + pd.offsets.MonthBegin(1)).date()  # first full VIIRS month
    with duckdb.connect() as con:
        n = dict(con.execute(f"""
            SELECT s, count(*) FROM read_parquet({lst}) WHERE d >= DATE '{ov0}' GROUP BY s""").fetchall())
        pre = dict(con.execute(f"""
            SELECT s, count(*) FROM read_parquet({lst})
            WHERE d >= DATE '{ov0}' AND d < DATE '{TERRA_DRIFT.date()}' GROUP BY s""").fetchall())
    return {
        "k_world": round(n[1] / n[0], 4),
        "k_terra_world": round(pre[1] / pre[2], 4),
        "countries": sorted(f.parent.name for f in files),
        "fire_cell_days": {"viirs": n[1], "modis": n[0], "viirs_pre_drift": pre[1], "terra_pre_drift": pre[2]},
        "computed": dt.date.today().isoformat(),
    }


def main():
    files = sorted(DATA.glob("countries/*/grid_daily.parquet"))
    if not files:
        sys.exit("no processed countries")
    out = compute(files)
    PRIOR_FILE.write_text(json.dumps(out, indent=1) + "\n")
    print(f"k_world={out['k_world']}  k_terra_world={out['k_terra_world']}  from {len(files)} countries")


if __name__ == "__main__":
    main()
