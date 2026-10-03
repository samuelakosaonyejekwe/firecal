"""Compute the worldwide MODIS->VIIRS calibration prior and the sensor outage days; save them to app/resources/prior.json.

The prior is the pooled VIIRS / MODIS (and VIIRS / Terra-only) fire cell-day ratio over the 2012+ overlap years
across all processed countries. Small or quiet areas lean on it, so it is kept fixed and versioned in the
repository: every machine and the website use the same value, and an area's results don't change when unrelated
countries are loaded.

The same file lists the days a satellite recorded far less than usual: under GAP_SHARE of its usual share of the
fire record (the median over the surrounding 61 days). These are instrument or data outages, not quiet days:
- viirs_gaps:   S-NPP VIIRS out worldwide (VIIRS/MODIS ratio, on days MODIS saw at least GAP_MIN_MODIS cells);
- viirs_strips: VIIRS out over a STRIP_DEG band of longitude (a lost orbit), on days VIIRS recorded under
                STRIP_SHARE of its usual ratio there while MODIS saw at least STRIP_MIN_MODIS cells;
- modis_gaps:   MODIS out worldwide, or Terra's part of it (Terra's share of MODIS);
- aqua_gaps:    Aqua's part of MODIS out (MODIS then holds Terra alone).
The analysis (app/analysis.py, app/static/engine.js) reads each day from the sensor that recorded it and leaves
these days out of every calibration; days with no usable record at all are estimated from the area's usual fire
for the date, at the level of the same month's recorded days, never read as "no fire". Re-run it deliberately
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
from app.constants import AQUA_START, CELL, PRIOR_FILE, TERRA_DRIFT, VIIRS_START  # noqa: E402

DATA = pathlib.Path(os.environ.get("FIRECAL_DATA", ROOT / "data"))
GAP_SHARE = 0.25        # a sensor is out below this share of its usual share of the record...
GAP_MIN_MODIS = 1000    # ...on a day MODIS saw at least this many fire cells worldwide (a normal fire day)
STRIP_DEG = 10          # VIIRS outage strips: bands of longitude this wide
STRIP_SHARE = 0.1       # a band whose VIIRS/MODIS ratio fell under this share of its usual one (stricter than
STRIP_MIN_MODIS = 100   # GAP_SHARE: weather moves a band's ratio far more than the world's), with this much MODIS


def _usual(x: pd.Series) -> pd.Series:
    return x.rolling(61, center=True, min_periods=15).median()


def viirs_gaps(daily: pd.DataFrame) -> list[str]:
    """Days VIIRS was out worldwide. `daily`: worldwide fire cells per day, columns 0 (MODIS) and 1 (VIIRS)."""
    d = daily[daily.index >= VIIRS_START].sort_index()
    ratio = d[1] / d[0]
    return [i.date().isoformat() for i in d.index[(d[0] >= GAP_MIN_MODIS) & (ratio < GAP_SHARE * _usual(ratio))]]


def viirs_strips(bands: pd.DataFrame, gaps: list[str]) -> dict[str, list[int]]:
    """Bands of longitude (floor(lon / STRIP_DEG)) where VIIRS was out on a day it worked elsewhere.
    `bands`: fire cells per day, band and sensor (columns d, b, s, n)."""
    out: dict[str, list[int]] = {}
    idx = pd.date_range(VIIRS_START, bands.d.max())
    for b, g in bands.groupby("b"):
        w = g.pivot_table(index="d", columns="s", values="n", aggfunc="sum", fill_value=0)
        w = w.reindex(idx, fill_value=0).reindex(columns=[0, 1], fill_value=0)
        ratio = w[1] / w[0]
        for d in w.index[(w[0] >= STRIP_MIN_MODIS) & (ratio < STRIP_SHARE * _usual(ratio))]:
            if d.date().isoformat() not in gaps:
                out.setdefault(d.date().isoformat(), []).append(int(b))
    return {d: sorted(v) for d, v in sorted(out.items())}


def modis_gaps(daily: pd.DataFrame) -> tuple[list[str], list[str]]:
    """(Days MODIS or its Terra part was out, days its Aqua part was out), worldwide.
    `daily`: fire cells per day, columns 0 (MODIS: Terra + Aqua) and 2 (Terra)."""
    m, t = daily[0], daily[2]
    out = (m < GAP_SHARE * _usual(m)) & (_usual(m) >= GAP_MIN_MODIS)
    aqua_era = (daily.index >= AQUA_START) & ~out  # Terra's and Aqua's shares, from the days both could fly
    terra = (t / m).where(aqua_era)
    aqua = 1 - terra
    out |= aqua_era & (terra < GAP_SHARE * _usual(terra))
    aqua_out = aqua_era & ~out & (aqua < GAP_SHARE * _usual(aqua))
    iso = lambda mask: [i.date().isoformat() for i in daily.index[mask]]  # noqa: E731
    return iso(out), iso(aqua_out)


def compute(files: list[pathlib.Path]) -> dict:
    """Pooled VIIRS/MODIS and VIIRS/Terra-only fire cell-day ratios over the 2012+ overlap, outage days excluded."""
    lst = "[" + ",".join(f"'{f.as_posix()}'" for f in files) + "]"
    ov0 = (VIIRS_START + pd.offsets.MonthBegin(1)).date()  # first full VIIRS month
    with duckdb.connect() as con:
        bands = con.execute(f"""SELECT d, s, floor(xi / {STRIP_DEG * CELL})::INT AS b, count(*) AS n
                                FROM read_parquet({lst}) GROUP BY ALL""").df()
    bands["d"] = pd.to_datetime(bands["d"])
    daily = bands.pivot_table(index="d", columns="s", values="n", aggfunc="sum", fill_value=0)
    daily = daily.reindex(pd.date_range(daily.index.min(), daily.index.max()), fill_value=0).reindex(columns=[0, 1, 2], fill_value=0)
    gaps = viirs_gaps(daily)
    strips = viirs_strips(bands[bands.s < 2], gaps)
    mgaps, agaps = modis_gaps(daily)
    # pooled ratios over the overlap, without any outage: the days a sensor was out, and the lost VIIRS strips
    ok = bands[(bands.d >= pd.Timestamp(ov0)) & ~bands.d.dt.strftime("%Y-%m-%d").isin(set(gaps) | set(mgaps) | set(agaps))]
    lost = pd.DataFrame([(pd.Timestamp(d), b) for d, bs in strips.items() for b in bs], columns=["d", "b"])
    ok = ok.merge(lost.assign(lost=True), on=["d", "b"], how="left")
    ok = ok[ok["lost"].isna()]
    n = ok.groupby("s").n.sum()
    pre = ok[ok.d < TERRA_DRIFT].groupby("s").n.sum()
    return {
        "k_world": round(n[1] / n[0], 4),
        "k_terra_world": round(pre[1] / pre[2], 4),
        "countries": sorted(f.parent.name for f in files),
        "fire_cell_days": {"viirs": int(n[1]), "modis": int(n[0]), "viirs_pre_drift": int(pre[1]), "terra_pre_drift": int(pre[2])},
        "viirs_gaps": gaps,
        "strip_deg": STRIP_DEG,
        "viirs_strips": strips,
        "modis_gaps": mgaps,
        "aqua_gaps": agaps,
        "computed": dt.date.today().isoformat(),
    }


def main():
    files = sorted(DATA.glob("countries/*/grid_daily.parquet"))
    if not files:
        sys.exit("no processed countries")
    out = compute(files)
    PRIOR_FILE.write_text(json.dumps(out, indent=1) + "\n")
    print(f"k_world={out['k_world']}  k_terra_world={out['k_terra_world']}  from {len(files)} countries, "
          f"outages: VIIRS {len(out['viirs_gaps'])} days + {len(out['viirs_strips'])} days in strips, "
          f"MODIS {len(out['modis_gaps'])} days, Aqua {len(out['aqua_gaps'])} days")


if __name__ == "__main__":
    main()
