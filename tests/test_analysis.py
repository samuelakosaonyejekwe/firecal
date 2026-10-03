"""Harmonization maths on synthetic data with a known answer."""
import json
import pathlib

import numpy as np
import pandas as pd
import pytest

from app.analysis import NeedsData, Store
from app.constants import VIIRS_START

RES = pathlib.Path(__file__).resolve().parent.parent / "app" / "resources"


def make_country(root, cid, k=3.0, kt=8.0, seed=0, season=(12, 1, 2), viirs_off=()):
    """Synthetic fires inside `cid`'s bbox: VIIRS sees exactly k× MODIS cell-days (none on `viirs_off` days)."""
    viirs_off = set(pd.DatetimeIndex(viirs_off))
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
        for i in range(int(round(m * (k / kt if d < VIIRS_START else 1)))):  # Terra's part (kt-scaled before VIIRS)
            rows.append((d, y0 + i % 5, x0 + i // 5, 2))
        if d >= VIIRS_START and d not in viirs_off:
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
    assert int(a["season_start"][:2]) not in (12, 1, 2)  # the season isn't split across the new year


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


def test_viirs_outage_days_are_filled_from_modis_not_read_as_no_fire(tmp_path):
    from app.analysis import VIIRS_GAPS
    assert len(VIIRS_GAPS) >= 30  # the outages found worldwide (prior.json)
    make_country(tmp_path, "Nigeria", viirs_off=VIIRS_GAPS)  # VIIRS saw nothing on those days, as in the real record
    res = Store(tmp_path, RES).analyze({"country": "Nigeria"})
    h, d = res["harmonization"], res["daily"]
    assert h["viirs_gaps"] == [g.date().isoformat() for g in VIIRS_GAPS]
    assert h["k_all"] == pytest.approx(3.0, rel=0.03)  # the outages don't drag the calibration down
    start = pd.Timestamp(d["start"])
    for g in VIIRS_GAPS:
        i = (g - start).days
        assert d["v"][i] is None and d["h"][i] == pytest.approx(d["m"][i] * h["k_month"][g.month - 1], abs=0.02)


def test_modis_outages_are_not_read_as_no_fire(store):
    """No record at all (MODIS out before VIIRS) -> the usual fire of those days, at the level of the month's
    recorded days; Aqua out -> Terra × k_terra."""
    from app.analysis import AQUA_OUT, MODIS_OUT
    res = store.analyze({"country": "Nigeria"})
    h, d = res["harmonization"], res["daily"]
    start = pd.Timestamp(d["start"])
    lost = pd.date_range("2001-06-16", "2001-06-30")
    assert set(lost.date.astype(str)) <= set(h["no_record"]) and lost.isin(MODIS_OUT).all()
    june = [d["h"][(day - start).days] for day in pd.date_range("2001-06-01", "2001-06-30")]
    seen = [x for day, x in zip(pd.date_range("2001-06-01", "2001-06-30"), june) if day not in MODIS_OUT]
    filled = [x for day, x in zip(pd.date_range("2001-06-01", "2001-06-30"), june) if day in MODIS_OUT]
    assert np.mean(filled) == pytest.approx(np.mean(seen), rel=0.5)  # a steady month: about the recorded days' rate
    assert h["no_record_scale"]["2001-06"] == pytest.approx(sum(june) / sum(seen), rel=1e-3)
    series, *_ = store.series({"country": "Nigeria"})
    for day in AQUA_OUT[AQUA_OUT < VIIRS_START]:
        i = (day - start).days
        assert day.date().isoformat() in h["terra_only"]
        assert d["h"][i] == pytest.approx(h["k_terra_month"][day.month - 1] * series.t.loc[day], abs=0.01)


def test_lost_viirs_orbit_is_filled_from_modis_in_that_strip(tmp_path):
    """2024-01-31: VIIRS lost the orbit over West Africa (prior.json viirs_strips); Nigeria's synthetic fires lie in it."""
    make_country(tmp_path, "Nigeria", viirs_off=[pd.Timestamp("2024-01-31")])
    res = Store(tmp_path, RES).analyze({"country": "Nigeria"})
    h, d = res["harmonization"], res["daily"]
    i = (pd.Timestamp("2024-01-31") - pd.Timestamp(d["start"])).days
    assert "2024-01-31" in h["viirs_strips"] and d["m"][i] > 0
    assert d["h"][i] == pytest.approx(d["m"][i] * h["k_month"][0], abs=0.02)
    assert h["k_all"] == pytest.approx(3.0, rel=0.03)


def test_highest_risk_weeks_wrap_around_new_year_and_have_fire(store):
    weeks = [pd.to_datetime(f"{w} 2001", format="%d %b %Y") for w in store.analyze({"country": "Nigeria"})["top_weeks"]]
    assert weeks
    for i, a in enumerate(weeks):
        for b in weeks[i + 1:]:
            gap = abs(a.dayofyear - b.dayofyear)
            assert min(gap, 365 - gap) >= 21, (a, b)  # e.g. never both "31 Dec" and "07 Jan"
    assert weeks[0].month in (12, 1, 2)  # the busiest week is in the synthetic fire season (Dec–Feb)


def test_live_week_starting_on_29_february_is_compared_with_every_year(store):
    days = pd.date_range("2028-02-28", "2028-03-06")  # complete days: 29 Feb – 5 Mar
    nrt = pd.DataFrame({"d": days, "yi": 0, "xi": 0, "n": 1, "frp": 1.0})  # no fires in the area this week
    out = store.nowcast(nrt, {"country": "Nigeria"})
    assert out["days"][0] == "2028-02-29" and len(out["days"]) == 6
    assert out["n_years"] == 24  # 2001–2024, not only the leap years


def test_live_week_without_fires_in_a_box_with_industrial_heat(tmp_path):
    make_country(tmp_path, "Nigeria")
    pd.DataFrame({"yi": [90], "xi": [80]}).astype("int16").to_parquet(tmp_path / "countries" / "Nigeria" / "static_cells.parquet")
    days = pd.date_range("2025-03-01", "2025-03-08")
    nrt = pd.DataFrame({"d": days, "yi": 0, "xi": 0, "n": 1, "frp": 1.0})  # every live fire is far from the box
    out = Store(tmp_path, RES).nowcast(nrt, {"bbox": [8.4, 8.8, 8.9, 9.3]})
    assert out["available"] and out["total"] == 0 and out["static_cells_masked"] == 1


def test_season_start_moves_by_days_not_months():
    """Two almost equally quiet months (Russia: Dec vs Jan) must not flip the season start by a month."""
    from app.analysis import season_start
    keys = [d.strftime("%m-%d") for d in pd.date_range("2001-01-01", "2001-12-31")]
    doy = np.arange(365)
    # spring and summer fires over a winter trough that is lowest around 10 January, as in Russia
    base = 50 * np.exp(-((doy - 120) / 25.0) ** 2) + 40 * np.exp(-((doy - 220) / 30.0) ** 2) + 1.5 - np.cos(2 * np.pi * (doy - 10) / 365) / 2
    a, *_ = season_start(base, keys)
    assert a in ("01-09", "01-10", "01-11")
    rng = np.random.default_rng(3)
    for _ in range(20):  # small changes in the data (a year added or dropped)
        b, *_ = season_start(base * rng.uniform(.95, 1.05, 365), keys)
        gap = abs((pd.Timestamp(f"2001-{a}") - pd.Timestamp(f"2001-{b}")).days)
        assert min(gap, 365 - gap) <= 21, (a, b)  # days, never a month's jump


def test_season_start_in_a_long_stretch_without_fire_is_its_middle():
    from app.analysis import season_start
    keys = [d.strftime("%m-%d") for d in pd.date_range("2001-01-01", "2001-12-31")]
    v = np.zeros(365); v[90:240] = 10.0  # fires 1 April – 27 August only
    k, m, d = season_start(v, keys)
    assert (m, d) == (12, 14)  # the middle of the fireless 28 Aug – 31 Mar stretch (as the 61-day window sees it)


def test_map_layers_average_over_the_years_that_hold_each_month():
    from app.analysis import layer_years
    end = pd.Timestamp("2024-12-31")
    assert [layer_years(m, end) for m in (1, 10, 11, 12)] == [24, 24, 25, 25]  # the record starts Nov 2000
    assert layer_years(None, end) == pytest.approx(24.17, abs=0.01)
