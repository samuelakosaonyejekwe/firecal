"""Clean raw FIRMS hotspots and bin both sensors onto a common 0.1° daily grid.

Output: data/{country}/grid_daily.parquet, one row per (date, cell, sensor) with any fire:
    d      DATE      acquisition date (UTC)
    yi, xi SMALLINT  cell index = floor(lat*10), floor(lon*10)
    s      TINYINT   0 = MODIS (Terra+Aqua, 1 km), 1 = VIIRS (S-NPP, 375 m),
                     2 = MODIS Terra only (needed to calibrate Nov 2000 - Jul 2002,
                         before Aqua launched, when MODIS had half the overpasses)
    n      INTEGER   detections in the cell that day
    frp    FLOAT     summed fire radiative power (MW)

Cleaning rules:
  * type = 0 only (presumed vegetation fire) -> drops volcanoes, static industrial
    sources and offshore gas flares, which are not "burning activity".
  * MODIS confidence >= 30 (drops the low-confidence class).
  * VIIRS confidence in {nominal, high} (drops 'l', mostly sun-glint / edge artefacts).

Usage:  python pipeline/build.py --country Nigeria
"""
import argparse
import os
import pathlib

import duckdb

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = pathlib.Path(os.environ.get("FIRECAL_DATA", ROOT / "data"))
CELL = 10  # cells per degree -> 0.1°

LOAD = """
CREATE TEMP TABLE modis AS
  SELECT acq_date, latitude, longitude, frp, satellite, confidence, type
  FROM read_csv('{raw}/modis_*.csv', union_by_name = true,
                types = {{'confidence': 'INTEGER', 'type': 'INTEGER'}});
CREATE TEMP TABLE viirs AS
  SELECT acq_date, latitude, longitude, frp, confidence, type
  FROM read_csv('{raw}/viirs-snpp_*.csv', union_by_name = true,
                types = {{'confidence': 'VARCHAR', 'type': 'INTEGER'}});
"""

GRID = """
COPY (
  WITH tagged AS (
    SELECT acq_date, latitude, longitude, frp, 0 AS s FROM modis WHERE type = 0 AND confidence >= 30
    UNION ALL SELECT acq_date, latitude, longitude, frp, 1 FROM viirs WHERE type = 0 AND confidence IN ('n', 'h')
    UNION ALL SELECT acq_date, latitude, longitude, frp, 2 FROM modis
              WHERE type = 0 AND confidence >= 30 AND satellite = 'Terra'
  )
  SELECT CAST(acq_date AS DATE) AS d,
         CAST(floor(latitude  * {cell}) AS SMALLINT) AS yi,
         CAST(floor(longitude * {cell}) AS SMALLINT) AS xi,
         CAST(s AS TINYINT) AS s,
         CAST(count(*) AS INTEGER) AS n,
         CAST(sum(frp) AS FLOAT) AS frp
  FROM tagged
  GROUP BY ALL
  ORDER BY 1, 2, 3
) TO '{out}' (FORMAT parquet, COMPRESSION zstd)
"""

# Cells dominated by non-vegetation heat (volcano / static industrial / offshore flares).
# Near-real-time FIRMS files carry no `type` column, so the live early-warning panel
# uses this mask to drop e.g. Niger Delta gas flares.
STATIC = """
COPY (
  SELECT CAST(floor(latitude * {cell}) AS SMALLINT) AS yi,
         CAST(floor(longitude * {cell}) AS SMALLINT) AS xi
  FROM viirs
  GROUP BY ALL
  HAVING count(*) FILTER (type <> 0) >= 20
     AND count(*) FILTER (type <> 0) >= count(*) FILTER (type = 0)
) TO '{out}' (FORMAT parquet)
"""


def build_country(country: str, keep_raw: bool = True) -> str:
    raw = DATA / "raw" / country
    out_dir = DATA / "countries" / country
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "grid_daily.parquet"
    tmp = out_dir / "grid_daily.parquet.tmp"
    if not any(raw.glob("viirs-snpp_*.csv")):
        raise RuntimeError(f"no VIIRS archive for {country!r}: cannot harmonize")

    con = duckdb.connect()
    con.execute(LOAD.format(raw=raw.as_posix()))
    con.execute(GRID.format(cell=CELL, out=tmp.as_posix()))
    con.execute(STATIC.format(cell=CELL, out=(out_dir / "static_cells.parquet").as_posix()))
    tmp.replace(out)  # atomic: the web app never sees a half-written file
    summary = con.execute(f"""
        SELECT CASE s WHEN 0 THEN 'MODIS' WHEN 1 THEN 'VIIRS' ELSE 'MODIS Terra-only' END AS sensor,
               min(d) AS first, max(d) AS last,
               count(*) AS fire_cell_days, sum(n) AS detections
        FROM '{out.as_posix()}' GROUP BY s ORDER BY s
    """).df().to_string(index=False)
    for f in (out_dir / "cache").glob("*.json"):  # analyses of the old grid are stale
        f.unlink()
    if not keep_raw:
        for f in raw.glob("*.csv"):
            f.unlink()
    return f"{summary}\nwrote {out} ({out.stat().st_size / 1e6:.1f} MB)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="Nigeria")
    ap.add_argument("--drop-raw", action="store_true", help="delete the raw CSVs after building")
    args = ap.parse_args()
    print(build_country(args.country, keep_raw=not args.drop_raw))


if __name__ == "__main__":
    main()
