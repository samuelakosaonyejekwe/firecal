"""Harmonization + burning-calendar analytics over the gridded FIRMS record — worldwide.

Unit of burning activity: the **fire cell-day** — a 0.1° grid cell with at least one
active-fire detection on a given day. Counting cells instead of raw detections
removes most of the resolution effect (one large fire = hundreds of 375 m VIIRS
pixels but a handful of 1 km MODIS pixels, yet one cell either way).

Harmonization (MODIS -> VIIRS-equivalent), fitted for *each* area of interest:
  VIIRS still sees more small fires, so even cell-days differ. During the overlap
  period (2012 -> present) both sensors observed the same fires, so we fit how many
  VIIRS cell-days correspond to one MODIS cell-day, per calendar month (fire type and
  size change through the season), with two levels of shrinkage so that small or
  quiet areas stay stable:

      k_area  = (ΣV + Λ·k_world) / (ΣM + Λ)                  area ratio -> global prior
      k_month = (ΣV_month + λ·k_area) / (ΣM_month + λ)       month ratio -> area ratio

  The harmonized record is VIIRS where VIIRS exists and k_month · MODIS before it,
  giving one consistent series from 2000 to present. Skill is reported with
  leave-one-year-out cross-validation over the overlap years.

  Before Aqua (Nov 2000 - Jul 2002) only Terra flew, so MODIS saw ~half the daily
  overpasses. That period gets its own factor fitted as Terra-only -> VIIRS over the
  same overlap years, otherwise 2001 would look artificially quiet.

Areas of interest are either a FIRMS country (its own file, exact FIRMS borders) or a
lon/lat box anywhere; a box combines every processed country it touches, merging
border cells so nothing is double-counted.
"""
from __future__ import annotations

import calendar
import concurrent.futures as cf
import hashlib
import json
import math
import pathlib
import shutil
import threading
from collections import OrderedDict

import duckdb
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import box, shape

from .constants import (AQUA_START, CELL, LAMBDA, LAMBDA_AREA, MAX_CV_ERROR, MAX_MAP_CELLS, MIN_OVERLAP_CELL_DAYS,
                        MIN_R2, MODIS_START, PRIOR_FILE, TERRA_DRIFT, VIIRS_START, load_prior, load_viirs_gaps)

# fingerprint of the analysis code: cached results are invalidated whenever the method changes
# (the prior file too: its worldwide ratios and VIIRS outage days change every result)
CODE_VERSION = hashlib.sha1(b"".join(
    (pathlib.Path(__file__).parent / f).read_bytes() for f in ("analysis.py", "constants.py")) + PRIOR_FILE.read_bytes()).hexdigest()[:10]
VIIRS_GAPS = load_viirs_gaps()


# ---- map layers (shared by the server and the website build, pipeline/static_site.py) ----
def layer_years(month, end) -> float:
    """Years a multi-year map layer averages over: those whose `month` is in the record, or the record's length."""
    if month:
        return sum(MODIS_START <= pd.Timestamp(y, int(month), 1) <= end for y in range(MODIS_START.year, end.year + 1))
    return ((end - MODIS_START).days + 1) / 365.2425


def vequiv_sql(k, kt) -> str:
    """One grid row's VIIRS-equivalent fire day: VIIRS itself, or calibrated MODIS before it and on its outage days."""
    gaps = ",".join(f"DATE '{d.date()}'" for d in VIIRS_GAPS)
    viirs = f"d >= DATE '{VIIRS_START.date()}'" + (f" AND d NOT IN ({gaps})" if gaps else "")
    return f"""CASE WHEN {viirs} THEN (s = 1)::INT * 1.0
                    WHEN d >= DATE '{AQUA_START.date()}' THEN (s = 0)::INT * {k}
                    ELSE (s = 2)::INT * {kt} END"""
GRID_WORKERS = 6       # countries computed in parallel for a map layer


def _df(sql: str) -> pd.DataFrame:
    """Run a query on a private connection (DuckDB's default connection isn't thread-safe)."""
    with duckdb.connect() as con:
        return con.execute(sql).df()


def _rows(sql: str) -> list:
    with duckdb.connect() as con:
        return con.execute(sql).fetchall()


def _r(x, nd=2):
    """Round for JSON; NaN/inf -> None."""
    if x is None:
        return None
    x = float(x)
    return None if not math.isfinite(x) else round(x, nd)


def _rl(a, nd=2):
    return [_r(v, nd) for v in a]


class NeedsData(Exception):
    """The area touches countries whose archives haven't been processed yet."""

    def __init__(self, missing):
        super().__init__(f"needs data for {missing}")
        self.missing = missing


_MISS = object()


class LRU(OrderedDict):
    """Least-recently-used memory cache, safe to share between request threads."""

    def __init__(self, n):
        super().__init__()
        self.n = n
        self._put_lock = threading.Lock()  # map layers fill it from several threads

    def put(self, k, v):
        with self._put_lock:
            self[k] = v
            self.move_to_end(k)
            while len(self) > self.n:
                self.popitem(last=False)
        return v

    def lookup(self, k):
        """The cached value (now the most recent), or _MISS; one step, so another thread can't evict it in between."""
        with self._put_lock:
            if k in self:
                self.move_to_end(k)
                return OrderedDict.__getitem__(self, k)
        return _MISS

    def memo(self, k, make):
        v = self.lookup(k)
        return self.put(k, make()) if v is _MISS else v


class Store:
    """All processed countries, queried as one global record."""

    def __init__(self, data_dir: pathlib.Path, res_dir: pathlib.Path):
        self.cdir = data_dir / "countries"
        self.cache_dir = data_dir / "cache"
        self.cdir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.meta = {c["id"]: c for c in json.loads((res_dir / "countries.json").read_text(encoding="utf-8"))}
        feats = json.loads((res_dir / "shapes.geojson").read_text(encoding="utf-8"))["features"]
        self._shape = {f["properties"]["id"]: shape(f["geometry"]) for f in feats}
        self._ids = list(self._shape)
        self._tree = shapely.STRtree([self._shape[i] for i in self._ids])
        for g in self._shape.values():
            shapely.prepare(g)
        self._lock = threading.RLock()
        self._mem = LRU(48)
        self._grids = LRU(256)
        self._ready: dict[str, float] = {}
        self._extent: dict[str, list] = {}
        self._extent_of: dict[str, float] = {}  # file time each extent was read at (a rebuilt country is re-read)
        self._wide_view: dict[str, list] = {}
        self._grid_locks: dict = {}
        self._prior = None
        self.end = pd.Timestamp(pd.Timestamp.now().year - 1, 12, 31)  # the last full year, until a country is processed
        self.refresh()

    # ------------------------------------------------------------ catalogue
    def path(self, cid):
        return self.cdir / cid / "grid_daily.parquet"

    def refresh(self):
        """Pick up newly processed countries."""
        with self._lock:
            ready = {}
            for p in self.cdir.glob("*/grid_daily.parquet"):
                try:
                    meta = p.parent / "built.json"  # its archive year can change the record's end (see below)
                    ready[p.parent.name] = (p.stat().st_mtime, meta.stat().st_mtime if meta.exists() else 0.0)
                except FileNotFoundError:  # removed between the glob and the stat
                    pass
            if ready == self._ready:
                return
            self._ready = ready
            self._mem.clear()
            self._grids.clear()
            for cid in [c for c in self._extent_of if c not in ready]:  # removed countries
                self._extent.pop(cid, None), self._wide_view.pop(cid, None), self._extent_of.pop(cid)
            for cid, mtime in ready.items():
                if self._extent_of.get(cid) == mtime:
                    continue
                self._extent_of[cid] = mtime
                self._extent.pop(cid, None), self._wide_view.pop(cid, None)
                e = _rows(f"SELECT min(xi), min(yi), max(xi), max(yi), max(d) FROM '{self.path(cid)}'")[0]
                if e[0] is not None:  # a country with no fire records at all has no data extent
                    self._extent[cid] = [e[0] / CELL, e[1] / CELL, (e[2] + 1) / CELL, (e[3] + 1) / CELL, pd.Timestamp(e[4])]
                    if (e[2] - e[0]) / CELL > 180:  # spans the 180° line (Russia, USA, Fiji, NZ): frame the main part
                        self._wide_view[cid] = self._main_side(cid)
            # One record end for every area: the earliest NASA yearly archive any country was built with. A country
            # still awaiting next year's rebuild must not show that year as zero fires, so a new year appears once
            # every country has it (pipeline/build.py records archive_through).
            years = [y for y in (self._archive_through(c) for c in ready) if y]
            if years:
                self.end = pd.Timestamp(min(years), 12, 31)

    def _archive_through(self, cid) -> int | None:
        """Last NASA yearly archive in this country's file (built.json), else its last year of data."""
        meta = self.cdir / cid / "built.json"
        try:
            y = json.loads(meta.read_text()).get("archive_through")
            if y:
                return int(y)
        except (FileNotFoundError, ValueError):
            pass
        return self._extent[cid][4].year if cid in self._extent else None

    @property
    def ready(self):
        return sorted(self._ready)

    def version(self, ids):
        return hashlib.sha1(json.dumps([(i, self._ready.get(i)) for i in sorted(ids)]).encode()).hexdigest()[:12]

    def _main_side(self, cid) -> list:
        """Extent of the side of the 180° meridian holding most of the country's fire cells."""
        rows = _rows(f"""SELECT xi >= 0 AS east, count(*) AS n, min(xi), min(yi), max(xi), max(yi)
                         FROM (SELECT DISTINCT xi, yi FROM '{self.path(cid)}') GROUP BY ALL ORDER BY n DESC""")
        _, _, x0, y0, x1, y1 = rows[0]
        return [x0 / CELL, y0 / CELL, (x1 + 1) / CELL, (y1 + 1) / CELL]

    def view(self, cid):
        if cid in self._wide_view:
            return [round(v, 2) for v in self._wide_view[cid]]
        if cid in self._extent:
            return [round(v, 2) for v in self._extent[cid][:4]]
        return self.meta.get(cid, {}).get("view")

    def countries_in(self, bbox):
        """FIRMS countries whose territory touches the box."""
        g = box(*bbox)
        hit = {self._ids[i] for i in self._tree.query(g, predicate="intersects")}
        for cid, e in self._extent.items():  # shapeless territories, by data extent
            if cid not in self._shape and not (e[2] < bbox[0] or e[0] > bbox[2] or e[3] < bbox[1] or e[1] > bbox[3]):
                hit.add(cid)
        return sorted(hit)

    def country_at(self, lon, lat):
        idx = self._tree.query(shapely.Point(lon, lat), predicate="intersects")
        return self._ids[idx[0]] if len(idx) else None

    def resolve(self, aoi: dict):
        """-> (files, country ids used, missing ids, bbox or None, label)."""
        if aoi.get("country"):
            cid = aoi["country"]
            if cid not in self.meta:
                raise KeyError(cid)
            if cid not in self._ready:
                raise NeedsData([cid])
            return [self.path(cid)], [cid], [], None, self.meta[cid]["name"]
        b = aoi["bbox"]
        ids = self.countries_in(b)
        have = [c for c in ids if c in self._ready]
        missing = [c for c in ids if c not in self._ready]
        if not have:
            raise NeedsData(missing)
        label = f"{abs(b[1]):.1f}°{'S' if b[1] < 0 else 'N'}–{abs(b[3]):.1f}°{'S' if b[3] < 0 else 'N'}, " \
                f"{abs(b[0]):.1f}°{'W' if b[0] < 0 else 'E'}–{abs(b[2]):.1f}°{'W' if b[2] < 0 else 'E'}"
        return [self.path(c) for c in have], have, missing, b, label

    @staticmethod
    def _cell_bounds(bbox):
        w, s, e, n = bbox
        return (math.floor(s * CELL), math.ceil(n * CELL) - 1, math.floor(w * CELL), math.ceil(e * CELL) - 1)

    def _src(self, files, bbox):
        """SQL fragment yielding one row per (d, cell, sensor), merged across files."""
        lst = "[" + ",".join(f"'{p.as_posix()}'" for p in files) + "]"
        where = ""
        if bbox:
            y0, y1, x0, x1 = self._cell_bounds(bbox)
            where = f"WHERE yi BETWEEN {y0} AND {y1} AND xi BETWEEN {x0} AND {x1}"
        if len(files) == 1:
            return f"(SELECT d, yi, xi, s, n, frp FROM read_parquet({lst}) {where})"
        return f"(SELECT d, yi, xi, s, sum(n) AS n, sum(frp) AS frp FROM read_parquet({lst}) {where} GROUP BY d, yi, xi, s)"

    def _daily_merged_sql(self, files, bbox) -> str:
        """Daily totals over several countries' files, each cell-day counted once (as _src's merge does).

        Equivalent to grouping every row by (d, cell, sensor) first, but much faster: totals are summed per
        file, and only cells that appear in more than one file (border strips) are corrected for the
        double count. Detections and FRP are plain sums either way."""
        lst = "[" + ",".join(f"'{p.as_posix()}'" for p in files) + "]"
        y0, y1, x0, x1 = self._cell_bounds(bbox)
        w = f"yi BETWEEN {y0} AND {y1} AND xi BETWEEN {x0} AND {x1}"
        return f"""
            WITH shared AS (SELECT yi, xi FROM (SELECT DISTINCT yi, xi, filename FROM read_parquet({lst}, filename = true)
                                                WHERE {w}) GROUP BY yi, xi HAVING count(*) > 1)
            SELECT d, s, sum(cells)::DOUBLE AS cells, sum(det)::DOUBLE AS det, sum(frp)::DOUBLE AS frp FROM (
              SELECT d, s, count(*) AS cells, sum(n) AS det, sum(frp) AS frp FROM read_parquet({lst}) WHERE {w} GROUP BY d, s
              UNION ALL
              SELECT d, s, count(DISTINCT (yi, xi)) - count(*) AS cells, 0 AS det, 0 AS frp
              FROM read_parquet({lst}) SEMI JOIN shared USING (yi, xi) WHERE {w} GROUP BY d, s
            ) GROUP BY d, s"""

    # ---------------------------------------------------------------- prior
    def prior(self):
        """Worldwide MODIS->VIIRS and Terra->VIIRS ratios: fixed and versioned (see pipeline/prior.py),
        so results never depend on which other countries happen to be loaded."""
        if self._prior is None:
            self._prior = load_prior()
        return self._prior

    # ---------------------------------------------------------------- series
    def daily(self, files, bbox) -> pd.DataFrame:
        if len(files) > 1 and bbox:
            df = _df(self._daily_merged_sql(files, bbox))
        else:
            df = _df(f"""
                SELECT d, s, count(*)::DOUBLE AS cells, sum(n)::DOUBLE AS det, sum(frp)::DOUBLE AS frp
                FROM {self._src(files, bbox)} GROUP BY d, s""")
        idx = pd.date_range(MODIS_START, self.end, freq="D")
        out = pd.DataFrame(index=idx)
        for s, col in ((0, "m"), (1, "v"), (2, "t")):
            part = df[df.s == s].set_index("d")
            part.index = pd.to_datetime(part.index)
            out[col] = part["cells"].reindex(idx).fillna(0.0)
            out[col + "_det"] = part["det"].reindex(idx).fillna(0.0)
            out[col + "_frp"] = part["frp"].reindex(idx).fillna(0.0)
        out.loc[idx < VIIRS_START, ["v", "v_det", "v_frp"]] = np.nan
        out.loc[idx.isin(VIIRS_GAPS), ["v", "v_det", "v_frp"]] = np.nan  # VIIRS outage days: no record, not "no fire"
        return out

    @staticmethod
    def _overlap(monthly, src):
        return monthly[monthly.overlap & ((src != "t") | (monthly.index < TERRA_DRIFT))]

    @classmethod
    def _fit_k(cls, monthly, src, prior, exclude_year=None):
        ov = cls._overlap(monthly, src)
        if exclude_year is not None:
            ov = ov[ov.index.year != exclude_year]
        k_area = (ov.v.sum() + LAMBDA_AREA * prior) / (ov[src].sum() + LAMBDA_AREA)
        by = ov.groupby(ov.index.month)[[src, "v"]].sum().reindex(range(1, 13), fill_value=0.0)
        k = ((by.v + LAMBDA * k_area) / (by[src] + LAMBDA)).to_numpy()
        return k_area, k

    @classmethod
    def _cv(cls, monthly, src, prior):
        """Leave-one-year-out: predict each overlap year's VIIRS total from `src` alone."""
        ov = cls._overlap(monthly, src)
        out = []
        for y in sorted(set(ov.index.year)):
            g = ov[ov.index.year == y]
            if len(g) < 12:
                continue
            _, ky = cls._fit_k(monthly, src, prior, exclude_year=y)
            pred = float((g[src] * ky[g.index.month - 1]).sum())
            obs = float(g.v.sum())
            out.append({"year": y, "obs": _r(obs, 1), "pred": _r(pred, 1), "raw": _r(float(g[src].sum()), 1),
                        "ape": _r(abs(pred - obs) / obs * 100, 1) if obs > 0 else None})
        apes = [c["ape"] for c in out if c["ape"] is not None]
        return out, (_r(np.median(apes), 1) if apes else None)

    def harmonize(self, day: pd.DataFrame):
        pm, pt = self.prior()
        # VIIRS outage days (no reference record): MODIS stands in for them, scaled like the pre-VIIRS years,
        # and they are left out of every comparison of the sensors (fits, R², cross-validation, scatter)
        gap = np.asarray((day.index >= VIIRS_START) & day.v.isna())
        fit = day[["m", "t", "v"]].copy()
        fit.loc[gap, ["m", "t"]] = np.nan
        monthly = fit.resample("MS").sum(min_count=1)
        monthly["overlap"] = (monthly.index >= (VIIRS_START + pd.offsets.MonthBegin(0))) & monthly.v.notna()
        k_all, k = self._fit_k(monthly, "m", pm)
        kt_all, kt = self._fit_k(monthly, "t", pt)
        mo = day.index.month - 1
        day["h"] = np.select([day.index < AQUA_START, (day.index < VIIRS_START) | gap], [day.t * kt[mo], day.m * k[mo]], day.v)

        ov = monthly[monthly.overlap].copy()
        ov["pred"] = ov.m * k[ov.index.month - 1]
        ss_res = float(((ov.v - ov.pred) ** 2).sum())
        ss_tot = float(((ov.v - ov.v.mean()) ** 2).sum())
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None
        cv, cv_ape = self._cv(monthly, "m", pm)
        _, cv_ape_t = self._cv(monthly, "t", pt)

        # VIIRS-independent check: in the Terra+Aqua years both MODIS estimates exist
        yr = pd.DataFrame({"a": day.m * k[mo], "b": day.t * kt[mo]})
        yr = yr[(yr.index >= "2003-01-01") & (yr.index < VIIRS_START)].resample("YS").sum()
        yr = yr[(yr.index.year < VIIRS_START.year) & (yr.a > 0)]
        terra_check = _r(float(np.median((yr.b - yr.a).abs() / yr.a * 100)), 1) if len(yr) else None
        n_ov = float(ov.v.sum())
        return {
            "k_all": _r(k_all, 3), "k_month": _rl(k, 3),
            "k_terra_all": _r(kt_all, 3), "k_terra_month": _rl(kt, 3),
            "k_world": _r(pm, 3), "k_terra_world": _r(pt, 3),
            "r2_monthly": _r(r2, 3),
            "cv": cv, "cv_median_ape": cv_ape, "cv_median_ape_terra": cv_ape_t,
            "terra_check_ape": terra_check,
            "viirs_gaps": [d.date().isoformat() for d in day.index[gap]],  # outage days filled from MODIS
            # "indicative only": few fires (small islands, deserts, humid forest), a poor monthly fit,
            # or a large out-of-sample error; see MAX_CV_ERROR
            "overlap_viirs_cell_days": _r(n_ov, 0),
            "low_counts": bool(n_ov < MIN_OVERLAP_CELL_DAYS or r2 is None or r2 < MIN_R2 or cv_ape is None
                               or cv_ape > MAX_CV_ERROR),
            "scatter": [[_r(a, 1), _r(b, 1), int(i.month)] for i, a, b in zip(ov.index, ov.m, ov.v)],
            "periods": [
                {"from": MODIS_START.date().isoformat(), "to": (AQUA_START - pd.Timedelta(days=1)).date().isoformat(),
                 "source": "MODIS Terra × k_terra"},
                {"from": AQUA_START.date().isoformat(), "to": (VIIRS_START - pd.Timedelta(days=1)).date().isoformat(),
                 "source": "MODIS Terra+Aqua × k"},
                {"from": VIIRS_START.date().isoformat(), "to": self.end.date().isoformat(), "source": "VIIRS S-NPP (reference)"},
            ],
        }

    def series(self, aoi):
        """Harmonized daily series for an AOI (memoized)."""
        files, ids, missing, bbox, label = self.resolve(aoi)
        key = ("series", json.dumps(aoi, sort_keys=True), self.version(ids), self.prior(), self.end)
        if (hit := self._mem.lookup(key)) is not _MISS:
            return hit
        day = self.daily(files, bbox)
        cal = self.harmonize(day)
        return self._mem.put(key, (day, cal, ids, missing, bbox, label))

    # --------------------------------------------------------------- calendar
    def analyze_bytes(self, aoi) -> bytes:
        """Analysis as ready-to-send JSON bytes, served straight from the disk cache when possible."""
        _, ids, _, _, _ = self.resolve(aoi)
        # key on the data version, the calibration prior (small areas lean on it) and the code version
        # (the record's end too: when the last country gains a new year, every area's results extend)
        key = json.dumps(aoi, sort_keys=True) + self.version(ids) + "%.6f/%.6f" % self.prior() + CODE_VERSION + str(self.end.date())
        cache = self.cache_dir / f"cal_{hashlib.sha1(key.encode()).hexdigest()[:20]}.json"
        if cache.exists():
            return cache.read_bytes()
        raw = json.dumps(self._analyze(aoi), separators=(",", ":")).encode()
        tmp = cache.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_bytes(raw)
        tmp.replace(cache)
        self._prune_cache()
        return raw

    def _prune_cache(self, keep=400):
        """Results from older code/data versions are never read again; keep the cache bounded."""
        def age(f):
            try:
                return f.stat().st_mtime
            except FileNotFoundError:  # another request pruned it a moment ago
                return 0.0
        files = sorted(self.cache_dir.glob("cal_*.json"), key=age, reverse=True)
        for f in files[keep:]:
            f.unlink(missing_ok=True)

    def analyze(self, aoi) -> dict:
        return json.loads(self.analyze_bytes(aoi))

    def _analyze(self, aoi) -> dict:
        day, cal, ids, missing, bbox, label = self.series(aoi)
        start, end = MODIS_START, self.end
        h = day.h
        h7 = h.rolling(7, center=True, min_periods=1).mean()
        first_full, last_full = start.year + 1, end.year
        full = (h.index.year >= first_full) & (h.index.year <= last_full)

        # ---- month × year matrix + anomalies (baseline = all complete years)
        mon = h.resample("MS").sum(min_count=1)
        years = list(range(start.year, end.year + 1))
        mat = np.full((len(years), 12), np.nan)
        for t, v in mon.items():
            mat[t.year - years[0], t.month - 1] = v
        base = mat[[years.index(y) for y in range(first_full, last_full + 1)]]
        mu, sd = np.nanmean(base, axis=0), np.nanstd(base, axis=0, ddof=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            z = (mat - mu) / np.where(sd > 0, sd, np.nan)

        # ignore off-season months (< 2% of a normal year's burning): tiny counts give huge z
        busy = mu >= max(1.0, 0.02 * np.nansum(mu))
        unusual = []
        for yi_, y in enumerate(years):
            for mo in range(12):
                zz = z[yi_, mo]
                if np.isfinite(zz) and abs(zz) >= 2 and busy[mo]:
                    unusual.append({"year": y, "month": mo + 1, "z": _r(zz), "value": _r(mat[yi_, mo], 1),
                                    "normal": _r(mu[mo], 1)})
        unusual.sort(key=lambda u: -abs(u["z"]))

        # ---- day-of-year climatology (Feb 29 folded into Feb 28)
        dd = pd.DataFrame({"h7": h7[full]})
        dd["key"] = dd.index.strftime("%m-%d").str.replace("02-29", "02-28")
        dd["year"] = dd.index.year
        piv = dd.groupby(["year", "key"]).h7.mean().unstack()
        vals = piv.to_numpy()
        clim = pd.DataFrame({"mean": np.nanmean(vals, 0), "p10": np.nanquantile(vals, 0.1, 0),
                             "p50": np.nanquantile(vals, 0.5, 0), "p90": np.nanquantile(vals, 0.9, 0)},
                            index=piv.columns)

        # ---- fire seasons: start the season the month after the quietest month
        s0 = int((np.nanargmin(mu) + 1) % 12) + 1 if np.isfinite(mu).any() else 1
        seasons = []
        y = start.year - 1
        while True:
            a = pd.Timestamp(y, s0, 1)
            b = pd.Timestamp(y + 1, s0, 1) - pd.Timedelta(days=1)
            y += 1
            if a < start:
                continue
            if b > end:
                break
            seg, seg7 = h[a:b], h7[a:b]
            tot = float(seg.sum())
            lab = str(a.year) if s0 == 1 else f"{a.year}–{str(a.year + 1)[2:]}"
            row = {"label": lab, "start_year": a.year, "from": a.date().isoformat(), "total": _r(tot, 1)}
            if tot >= 5:
                # tolerances keep dates independent of floating-point noise (and identical in engine.js)
                cs = seg.cumsum() / tot
                st, en = cs.index[cs >= 0.1 - 1e-9][0], cs.index[cs >= 0.9 - 1e-9][0]
                pk = seg7.round(9).idxmax()
                row.update(start=st.date().isoformat(), peak=pk.date().isoformat(), end=en.date().isoformat(),
                           start_off=(st - a).days, peak_off=(pk - a).days, end_off=(en - a).days,
                           length=(en - st).days + 1, from_modis=bool(a < VIIRS_START))
            seasons.append(row)
        tots = np.array([s["total"] for s in seasons], float)
        smu = tots.mean() if len(tots) else np.nan
        ssd = tots.std(ddof=1) if len(tots) > 1 else np.nan
        for s in seasons:
            s["z"] = _r((s["total"] - smu) / ssd) if ssd and ssd > 0 else None

        timed = [s for s in seasons if "start_off" in s]

        def off_label(off):
            return (pd.Timestamp(2001, s0, 1) + pd.Timedelta(days=int(round(off)))).strftime("%d %b")

        critical = None
        if len(timed) >= 3:
            med = {k: float(np.median([s[k] for s in timed])) for k in ("start_off", "peak_off", "end_off")}
            critical = {"start": off_label(med["start_off"]), "peak": off_label(med["peak_off"]),
                        "end": off_label(med["end_off"]), "length": int(med["end_off"] - med["start_off"] + 1),
                        **{k: _r(v, 0) for k, v in med.items()}}
        # four busiest weeks, at least 3 weeks apart: 7-day sums centred on each day of the mean year, wrapping
        # around New Year (31 Dec and 7 Jan are one week apart); ties go to the earlier date; weeks with fire only
        avg = clim["mean"].to_numpy(dtype=float)
        n = len(avg)
        wk = np.array([avg[[(i + j) % n for j in range(-3, 4)]].sum() for i in range(n)])
        doy = [pd.Timestamp(f"2001-{k}").dayofyear for k in clim.index]
        top = []
        for i in sorted(range(n), key=lambda i: (-wk[i], i)):
            if wk[i] <= 0 or len(top) == 4:
                break
            if all(min(abs(doy[i] - doy[j]), 365 - abs(doy[i] - doy[j])) >= 21 for j in top):
                top.append(i)
        top_weeks = [clim.index[i] for i in top]

        yearly = pd.DataFrame({"m": day.m, "v": day.v, "h": h}).resample("YS").sum(min_count=1)
        yearly = yearly[(yearly.index.year >= first_full) & (yearly.index.year <= last_full)]
        yearly.loc[yearly.index < VIIRS_START, "v"] = np.nan

        return {
            "aoi": aoi, "label": label, "countries": [{"id": c, "name": self.meta[c]["name"]} for c in ids],
            "missing": [{"id": c, "name": self.meta[c]["name"]} for c in missing],
            "view": bbox or self.view(ids[0]),
            "total_cell_days": _r(float(h.sum()), 0),
            "range": {"start": start.date().isoformat(), "end": end.date().isoformat(),
                      "viirs_start": VIIRS_START.date().isoformat(), "first_full": first_full, "last_full": last_full},
            "harmonization": cal,
            "daily": {
                "start": start.date().isoformat(),
                "m": _rl(day.m, 1), "v": _rl(day.v, 1), "h": _rl(h, 2), "h7": _rl(h7, 2),
                "frp": _rl(np.where(day.index < VIIRS_START, day.m_frp, day.v_frp), 0),
            },
            "monthly": {"years": years, "values": [_rl(r, 1) for r in mat], "z": [_rl(r) for r in z],
                        "normal": _rl(mu, 1), "sd": _rl(sd, 1)},
            "unusual": unusual[:12],
            "climatology": {"keys": list(clim.index), **{c: _rl(clim[c], 2) for c in clim.columns}},
            "season_start_month": s0,
            "seasons": seasons,
            "critical": critical,
            "top_weeks": [pd.Timestamp(f"2001-{k}").strftime("%d %b") for k in top_weeks],
            "yearly": {"years": [int(t.year) for t in yearly.index], "m": _rl(yearly.m, 0),
                       "v": _rl(yearly.v, 0), "h": _rl(yearly.h, 0)},
        }

    # -------------------------------------------------------------------- map
    def _country_grid(self, cid, year, month) -> pd.DataFrame:
        """Per-cell VIIRS-equivalent fire days for one country (memoized)."""
        key = (cid, self._ready.get(cid), year, month, self.prior(), self.end)
        if (hit := self._grids.lookup(key)) is not _MISS:
            return hit
        with self._lock:  # requests asking for the same layer at once share one computation
            lock = self._grid_locks.setdefault(key, threading.Lock())
        with lock:
            if (hit := self._grids.lookup(key)) is not _MISS:
                return hit
            return self._compute_grid(cid, year, month, key)

    def _compute_grid(self, cid, year, month, key) -> pd.DataFrame:
        # disk cache survives restarts; invalidated when the grid is rebuilt or the prior changes
        tag = hashlib.sha1(("%.6f/%.6f" % self.prior() + CODE_VERSION + str(self.end.date())).encode()).hexdigest()[:8]
        disk = self.cdir / cid / "grid_cache" / tag / f"{year or 'all'}_{month or 'all'}.parquet"
        if disk.exists() and disk.stat().st_mtime >= self.path(cid).stat().st_mtime:
            return self._grids.put(key, pd.read_parquet(disk))
        if not disk.parent.exists():  # first layer for this version: drop layers of older versions
            for old in disk.parent.parent.glob("*"):
                if old.is_dir() and old.name != tag:  # (another thread may have just created this version's folder)
                    shutil.rmtree(old, ignore_errors=True)
        k, kt = self._country_k(cid, disk.parent)
        where = [f"d <= DATE '{self.end.date()}'"]  # the record shown everywhere ends here (a country rebuilt early waits)
        if year:
            where.append(f"year(d) = {int(year)}")
        if month:
            where.append(f"month(d) = {int(month)}")
        w = ("AND " + " AND ".join(where)) if where else ""
        df = _df(f"""
            SELECT xi, yi, sum({vequiv_sql(k, kt)}) AS val
            FROM '{self.path(cid)}' WHERE true {w} GROUP BY xi, yi HAVING val > 0""")
        if not year:  # mean per year over the years that hold this month (or the whole record)
            df["val"] /= layer_years(month, self.end)
        disk.parent.mkdir(parents=True, exist_ok=True)
        tmp = disk.with_suffix(f".{threading.get_ident()}.tmp")
        df.to_parquet(tmp)
        tmp.replace(disk)
        return self._grids.put(key, df)

    def _country_k(self, cid, folder: pathlib.Path) -> tuple[float, float]:
        """The country's calibration ratios (all-years, Terra-only), kept on disk next to its map layers:
        computing them takes ~0.4 s per country, which every new layer would otherwise repeat."""
        f = folder / "k.json"
        if f.exists() and f.stat().st_mtime >= self.path(cid).stat().st_mtime:
            k = json.loads(f.read_text())
            return k["k"], k["kt"]
        _, cal, *_ = self.series({"country": cid})
        k, kt = cal["k_all"] or 1.0, cal["k_terra_all"] or 1.0
        folder.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"k": k, "kt": kt}))
        tmp.replace(f)
        return k, kt

    def grid(self, bbox, year=None, month=None) -> dict:
        """Map layer: fire days per 0.1° cell inside the viewport, over processed countries."""
        ids = [c for c in self.countries_in(bbox) if c in self._ready]
        y0, y1, x0, x1 = self._cell_bounds(bbox)
        with cf.ThreadPoolExecutor(max_workers=GRID_WORKERS) as pool:  # DuckDB scans run in parallel
            grids = list(pool.map(lambda c: self._country_grid(c, year, month), ids))
        parts = [g[(g.yi >= y0) & (g.yi <= y1) & (g.xi >= x0) & (g.xi <= x1)] for g in grids]
        df = pd.concat(parts) if parts else pd.DataFrame({"xi": [], "yi": [], "val": []})
        df = df.groupby(["xi", "yi"], as_index=False).val.max() if len(parts) > 1 else df
        # one color scale for the whole world (as on the website), so a color means the same everywhere
        return {**self._coarsen(df), "max": self._layer_max(year, month), "year": year, "month": month, "countries": ids}

    def _layer_max(self, year, month) -> float:
        """99th percentile of the layer's 0.1° cells over every available country (pipeline/static_site.py's rule)."""
        key = ("layermax", tuple(sorted(self._ready.items())), year, month, self.prior(), self.end)

        def compute():
            with cf.ThreadPoolExecutor(max_workers=GRID_WORKERS) as pool:
                grids = list(pool.map(lambda c: self._country_grid(c, year, month), self.ready))
            df = pd.concat(grids) if grids else pd.DataFrame({"xi": [], "yi": [], "val": []})
            vals = df.groupby(["xi", "yi"]).val.max() if len(grids) > 1 else df.val
            vals = vals[vals >= 0.05]  # below 0.05 a cell rounds to "no fire" on the website
            return _r(float(np.quantile(vals, 0.99)), 2) if len(vals) else 0
        return self._mem.memo(key, compute)

    @staticmethod
    def _coarsen(df, limit=MAX_MAP_CELLS, how="mean"):
        base, f = df, 1
        while len(df) > limit:
            f += 1
            df = base.assign(xi=base.xi // f, yi=base.yi // f).groupby(["xi", "yi"], as_index=False).val.sum()
            if how == "mean":
                df["val"] /= f * f  # keep "per 0.1° cell" units
        vals = df.val.to_numpy()
        return {"cell": f / CELL, "max": _r(np.quantile(vals, 0.99) if len(vals) else 0, 2),
                "cells": [[int(a), int(b), _r(c, 2)] for a, b, c in zip(df.xi, df.yi, df.val)]}

    # ------------------------------------------------------------- live/NRT
    def _own_cells(self, cid) -> set:
        key = ("own", cid, self._ready.get(cid))
        return self._mem.memo(key, lambda: set(map(tuple, _rows(f"SELECT DISTINCT yi, xi FROM '{self.path(cid)}'"))))

    def _all_static(self) -> pd.DataFrame | None:
        """Industrial-heat cells of every available country (yi, xi, _static), cached per data version."""
        key = ("static-all", tuple(sorted(self._ready.items())))

        def compute():
            files = [p.as_posix() for p in self.cdir.glob("*/static_cells.parquet")]
            return _df(f"SELECT DISTINCT yi, xi, 1 AS _static FROM read_parquet({files})") if files else None
        return self._mem.memo(key, compute)

    def static_cells(self, ids) -> set:
        out = set()
        for c in ids:
            p = self.cdir / c / "static_cells.parquet"
            if p.exists():
                out.update(map(tuple, _rows(f"SELECT yi, xi FROM '{p}'")))
        return out

    def live(self, nrt: pd.DataFrame, bbox) -> dict:
        y0, y1, x0, x1 = self._cell_bounds(bbox)
        g = nrt[(nrt.yi >= y0) & (nrt.yi <= y1) & (nrt.xi >= x0) & (nrt.xi <= x1)]
        mask = self._all_static()  # gas flares and industrial heat are not wildfires (as on the website)
        if mask is not None and len(g):
            g = g.merge(mask, on=["yi", "xi"], how="left")
            g = g[g._static.isna()]
        df = g.groupby(["xi", "yi"], as_index=False).n.sum().rename(columns={"n": "val"})
        return {**self._coarsen(df, limit=15000, how="sum"), "days": sorted({d.date().isoformat() for d in g.d})}

    def nowcast(self, nrt: pd.DataFrame, aoi) -> dict:
        """The feed's complete days (normally 6) vs the same calendar window in every past year."""
        # the feed is a rolling 168 h window: its first and last calendar days are partial
        days = pd.date_range(nrt.d.min() + pd.Timedelta(days=1), nrt.d.max() - pd.Timedelta(days=1))
        if not len(days):
            return {"available": False, "reason": "the live feed has no complete day yet"}
        if aoi.get("country"):
            cid = aoi["country"]
            boxes = [b for b in (self.meta[cid].get("bbox"), self._extent.get(cid, [None] * 4)[:4]) if b and b[0] is not None]
            if not boxes:
                return {"available": False, "reason": "no boundary for this territory"}
            # union of the Natural Earth bbox and the country's own data extent (borders differ, e.g. Cyprus)
            v = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
            y0, y1, x0, x1 = self._cell_bounds(v)
            g = nrt[(nrt.yi >= y0) & (nrt.yi <= y1) & (nrt.xi >= x0) & (nrt.xi <= x1)]
            if cid in self._shape:  # border test on cell centres
                inside = shapely.intersects_xy(self._shape[cid], (g.xi + 0.5) / CELL, (g.yi + 0.5) / CELL)
                if cid in self._ready:  # FIRMS and Natural Earth borders differ (e.g. Cyprus):
                    own = self._own_cells(cid)  # also keep cells in the country's own fire record
                    inside |= np.array([(a, b) in own for a, b in zip(g.yi, g.xi)], dtype=bool)
                g = g[inside]
            ids = [cid]
        else:
            y0, y1, x0, x1 = self._cell_bounds(aoi["bbox"])
            g = nrt[(nrt.yi >= y0) & (nrt.yi <= y1) & (nrt.xi >= x0) & (nrt.xi <= x1)]
            ids = self.countries_in(aoi["bbox"])
        static = self.static_cells([c for c in ids if c in self._ready])
        if static:
            g = g[[(a, b) not in static for a, b in zip(g.yi, g.xi)]]
        g = g[g.d.isin(days)]
        per_day = g.groupby("d").size().reindex(days, fill_value=0)
        now = int(per_day.sum())
        out = {"available": True, "days": [d.date().isoformat() for d in days], "cells": per_day.tolist(),
               "total": now, "frp": _r(float(g.frp.sum()), 0), "static_cells_masked": len(static)}
        try:
            day, *_ = self.series(aoi)
        except NeedsData:
            return {**out, "history": False}
        h = day.h
        hist = []
        # every past year's window starts on the same calendar date (28 Feb stands in for 29 Feb) and is exactly
        # as long as this week's, so leap days never compare 5 past days with 6 days now
        for y in range(MODIS_START.year + 1, self.end.year + 1):
            md = (days[0].month, days[0].day)
            a = pd.Timestamp(y, md[0], 28 if md == (2, 29) and not calendar.isleap(y) else md[1])
            b = a + pd.Timedelta(days=len(days) - 1)
            if b <= self.end:
                hist.append(float(h[a:b].sum()))
        hist = np.array(hist)
        if not len(hist):
            return {**out, "history": False}
        pct = float((hist < now).mean() * 100 + (hist == now).mean() * 50)
        return {**out, "history": True, "p10": _r(np.quantile(hist, 0.1), 1), "p50": _r(np.median(hist), 1),
                "p90": _r(np.quantile(hist, 0.9), 1), "max": _r(hist.max(), 1), "percentile": _r(pct, 0),
                "n_years": len(hist), "record": bool(now > hist.max())}
