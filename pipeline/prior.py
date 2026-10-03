"""Compute the worldwide MODIS->VIIRS calibration prior and save it to app/resources/prior.json.

The prior is the pooled VIIRS / MODIS (and VIIRS / Terra-only) fire cell-day ratio over the
2012+ overlap years across all processed countries. The same file lists the days S-NPP VIIRS was out:
worldwide it recorded under GAP_SHARE of its usual ratio to MODIS (the median over the surrounding
61 days) while MODIS saw at least GAP_MIN_MODIS fire cells. On those days the analysis fills the
record from MODIS instead of reading "no fire", and leaves them out of every calibration. Small or quiet areas lean on it, so it is
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
GAP_SHARE = 0.25      # a day is a VIIRS outage below this share of the usual VIIRS/MODIS ratio...
GAP_MIN_MODIS = 1000  # ...on a day MODIS saw at least this many fire cells worldwide (a normal fire day)


def viirs_gaps(daily: pd.DataFrame) -> list[str]:
    """Days VIIRS was out worldwide. `daily`: worldwide fire cells per day, columns 0 (MODIS) and 1 (VIIRS)."""
    d = daily[daily.index >= VIIRS_START].sort_index()
    ratio = d[1] / d[0]
    usual = ratio.rolling(61, center=True, min_periods=15).median()
    return [i.date().isoformat() for i in d.index[(d[0] >= GAP_MIN_MODIS) & (ratio < GAP_SHARE * usual)]]


def compute(files: list[pathlib.Path]) -> dict:
    """Pooled VIIRS/MODIS and VIIRS/Terra-only fire cell-day ratios over the 2012+ overlap, outage days excluded."""
    lst = "[" + ",".join(f"'{f.as_posix()}'" for f in files) + "]"
    ov0 = (VIIRS_START + pd.offsets.MonthBegin(1)).date()  # first full VIIRS month
    with duckdb.connect() as con:
        daily = con.execute(f"SELECT d, s, count(*) AS n FROM read_parquet({lst}) WHERE s < 2 GROUP BY d, s").df()
        daily["d"] = pd.to_datetime(daily["d"])
        gaps = viirs_gaps(daily.pivot_table(index="d", columns="s", values="n", fill_value=0))
        skip = "AND d NOT IN (" + ",".join(f"DATE '{g}'" for g in gaps) + ")" if gaps else ""
        n = dict(con.execute(f"""
            SELECT s, count(*) FROM read_parquet({lst}) WHERE d >= DATE '{ov0}' {skip} GROUP BY s""").fetchall())
        pre = dict(con.execute(f"""
            SELECT s, count(*) FROM read_parquet({lst})
            WHERE d >= DATE '{ov0}' AND d < DATE '{TERRA_DRIFT.date()}' {skip} GROUP BY s""").fetchall())
    return {
        "k_world": round(n[1] / n[0], 4),
        "k_terra_world": round(pre[1] / pre[2], 4),
        "countries": sorted(f.parent.name for f in files),
        "fire_cell_days": {"viirs": n[1], "modis": n[0], "viirs_pre_drift": pre[1], "terra_pre_drift": pre[2]},
        "viirs_gaps": gaps,
        "computed": dt.date.today().isoformat(),
    }


def main():
    files = sorted(DATA.glob("countries/*/grid_daily.parquet"))
    if not files:
        sys.exit("no processed countries")
    out = compute(files)
    PRIOR_FILE.write_text(json.dumps(out, indent=1) + "\n")
    print(f"k_world={out['k_world']}  k_terra_world={out['k_terra_world']}  from {len(files)} countries, "
          f"{len(out['viirs_gaps'])} VIIRS outage days excluded")


if __name__ == "__main__":
    main()
