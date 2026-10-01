"""Harmonization maths on synthetic data with a known answer."""
import json
import pathlib

import numpy as np
import pandas as pd
import pytest

from app.analysis import NeedsData, Store
from app.constants import AQUA_START, VIIRS_START

RES = pathlib.Path(__file__).resolve().parent.parent / "app" / "resources"


def make_country(root, cid, k=3.0, kt=8.0, seed=0, season=(12, 1, 2)):
    """Synthetic fires inside `cid`'s bbox: VIIRS sees exactly k× MODIS cell-days."""
    rng = np.random.default_rng(seed)
    meta = {c["id"]: c for c in json.loads((RES / "countries.json").read_text(encoding="utf-8"))}
    w, s, e, n = meta[cid]["view"]
    x0, y0 = int(np.ceil(w * 10)) + 2, int(np.ceil(s * 10)) + 2
    rows = []
    for d in pd.date_range("2000-11-01", "2024-12-31"):
        m = 20 if d.month in season else 1
        m = rng.poisson(m)
        for i in range(m):  # MODIS (Terra+Aqua) cells
            rows.append((d, y0 + i % 5, x0 + i // 5, 0))
        if d < AQUA_START or d >= VIIRS_START:
            for i in range(int(round(m * (k / kt if d < AQUA_START else 1)))):
                rows.append((d, y0 + i % 5, x0 + i // 5, 2))  # Terra-only
        if d >= VIIRS_START:
            for i in range(int(round(m * k))):
                rows.append((d, y0 + i % 7, x0 + 100 + i // 7, 1))  # VIIRS
    df = pd.DataFrame(rows, columns=["d", "yi", "xi", "s"])
    df["n"], df["frp"] = 1, 10.0
    df = df.astype({"yi": "int16", "xi": "int16", "s": "int8"})
    out = root / "countries" / cid
    out.mkdir(parents=True)
    df.to_parquet(out / "grid_daily.parquet")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    make_country(root, "Nigeria")
    return Store(root, RES)


def test_recovers_known_ratio(store):
    h = store.analyze({"country": "Nigeria"})["harmonization"]
    assert h["k_all"] == pytest.approx(3.0, rel=0.03)
    assert h["cv_median_ape"] < 5


def test_harmonized_series_is_continuous_across_sensor_switch(store):
    y = store.analyze({"country": "Nigeria"})["yearly"]
    h = dict(zip(y["years"], y["h"]))
    before, after = np.mean([h[t] for t in range(2003, 2012)]), np.mean([h[t] for t in range(2013, 2024)])
    assert after == pytest.approx(before, rel=0.08)  # no artificial jump in 2012


def test_season_and_critical_period(store):
    a = store.analyze({"country": "Nigeria"})
    assert a["critical"] is not None
    assert a["critical"]["peak"].split()[1] in {"Dec", "Jan", "Feb"}
    assert a["season_start_month"] not in (12, 1, 2)  # the season isn't split across the new year


def test_unprocessed_country_needs_data(store):
    with pytest.raises(NeedsData) as e:
        store.analyze({"country": "Ghana"})
    assert e.value.missing == ["Ghana"]


def test_box_touching_unprocessed_neighbour_is_partial(store):
    a = store.analyze({"bbox": [2.0, 6.0, 5.0, 9.0]})  # Nigeria + Benin
    assert "Nigeria" in [c["id"] for c in a["countries"]]
    assert "Benin" in [m["id"] for m in a["missing"]]


def test_ocean_box_has_nothing(store):
    with pytest.raises(NeedsData) as e:
        store.analyze({"bbox": [-30, -30, -25, -25]})
    assert e.value.missing == []


def test_multi_country_box_counts_shared_border_cells_once(tmp_path):
    """The fast multi-file daily query equals the full merge, including cells present in several files."""
    from app.analysis import _df
    rng = np.random.default_rng(4)
    files = []
    for i, cid in enumerate(("Nigeria", "Benin", "Niger")):
        n = 4000
        df = pd.DataFrame({"d": pd.to_datetime("2015-01-01") + pd.to_timedelta(rng.integers(0, 60, n), "D"),
                           "yi": rng.integers(90, 96, n), "xi": rng.integers(30 + i, 36 + i, n),  # overlapping cells
                           "s": rng.integers(0, 3, n), "n": rng.integers(1, 4, n), "frp": rng.uniform(0, 50, n)})
        df = df.drop_duplicates(["d", "yi", "xi", "s"]).astype({"yi": "int16", "xi": "int16", "s": "int8"})
        out = tmp_path / "countries" / cid
        out.mkdir(parents=True)
        df.to_parquet(out / "grid_daily.parquet")
        files.append(out / "grid_daily.parquet")
    s = Store(tmp_path, RES)
    bbox = [3.05, 9.05, 3.75, 9.45]  # cuts through the shared cells
    full = _df(f"SELECT d, s, count(*)::DOUBLE AS cells, sum(n)::DOUBLE AS det, sum(frp)::DOUBLE AS frp "
               f"FROM {s._src(files, bbox)} GROUP BY d, s").sort_values(["d", "s"]).reset_index(drop=True)
    fast = _df(s._daily_merged_sql(files, bbox)).sort_values(["d", "s"]).reset_index(drop=True)
    assert len(full) == len(fast) and (full.cells == fast.cells).all()
    assert np.allclose(full.det, fast.det) and np.allclose(full.frp, fast.frp)
    y0, y1, x0, x1 = s._cell_bounds(bbox)
    frames = [pd.read_parquet(f) for f in files]
    rows = sum(int(((g.yi >= y0) & (g.yi <= y1) & (g.xi >= x0) & (g.xi <= x1)).sum()) for g in frames)
    assert full.cells.sum() < rows  # the box really contains cell-days shared between files
