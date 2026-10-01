"""Near-real-time hotspots (last 7 days) for the server edition's early-warning panel.

Source: FIRMS global VIIRS S-NPP 375 m NRT feed — the same satellite and sensor as the
harmonized record's reference, so current activity is directly comparable with history.
The ~30 MB feed is downloaded at most every REFRESH seconds and reduced to 0.1° cell-days.
"""
from __future__ import annotations

import pathlib
import threading
import time
import urllib.request

import duckdb
import pandas as pd

from .constants import CELL, NRT_URL as URL

REFRESH = 3 * 3600


class NRTFeed:
    def __init__(self, data_dir: pathlib.Path):
        self.dir = data_dir / "nrt"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "viirs_snpp_7d.parquet"
        self._lock = threading.Lock()
        self._df: pd.DataFrame | None = None
        self._loaded_mtime = 0.0
        self.error: str | None = None

    def _refresh(self):
        csv = self.dir / "feed.csv.part"
        urllib.request.urlretrieve(URL, csv)
        tmp = self.path.with_suffix(".tmp")
        with duckdb.connect() as con:
            con.execute(f"""
            COPY (SELECT CAST(acq_date AS DATE) AS d,
                         CAST(floor(latitude * {CELL}) AS SMALLINT) AS yi,
                         CAST(floor(longitude * {CELL}) AS SMALLINT) AS xi,
                         CAST(count(*) AS INTEGER) AS n, CAST(sum(frp) AS FLOAT) AS frp
                  FROM read_csv('{csv.as_posix()}', types = {{'confidence': 'VARCHAR'}})
                  WHERE confidence IN ('nominal', 'high', 'n', 'h')
                  GROUP BY ALL) TO '{tmp.as_posix()}' (FORMAT parquet)""")
        tmp.replace(self.path)
        csv.unlink(missing_ok=True)

    def refresh_if_stale(self):
        age = time.time() - self.path.stat().st_mtime if self.path.exists() else float("inf")
        if age <= REFRESH:
            return
        try:
            self._refresh()
            self.error = None
        except Exception as e:  # offline / FIRMS down: keep serving the stale copy
            self.error = f"live feed unavailable ({e.__class__.__name__})"

    def start(self):
        """Keep the feed fresh from a daemon thread so requests never wait on the download."""
        def loop():
            while True:
                self.refresh_if_stale()
                time.sleep(600)
        threading.Thread(target=loop, daemon=True, name="nrt-refresh").start()

    def frame(self) -> pd.DataFrame | None:
        """Cell-days from the last good copy of the feed (None until the first download lands)."""
        with self._lock:
            if not self.path.exists():
                return None
            mtime = self.path.stat().st_mtime
            if self._df is None or mtime != self._loaded_mtime:
                with duckdb.connect() as con:
                    self._df = con.execute(f"SELECT * FROM '{self.path.as_posix()}'").df()
                self._df["d"] = pd.to_datetime(self._df["d"])
                self._loaded_mtime = mtime
            return self._df

    def fetched_at(self) -> str | None:
        if not self.path.exists():
            return None
        return pd.Timestamp(self.path.stat().st_mtime, unit="s", tz="UTC").isoformat(timespec="minutes")
