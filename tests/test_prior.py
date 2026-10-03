"""pipeline/prior.py: the outage rules find real sensor outages and leave ordinary days alone."""
import numpy as np
import pandas as pd

from app.constants import AQUA_START, VIIRS_START
from pipeline import prior


def world(days, seed=0):
    """Worldwide fire cells per day: MODIS (0) ~3000, VIIRS (1) = 2.6 × MODIS, Terra (2) = 40% of MODIS."""
    rng = np.random.default_rng(seed)
    m = rng.poisson(3000, len(days)).astype(float)
    return pd.DataFrame({0: m, 1: np.round(m * 2.6 * rng.uniform(.9, 1.1, len(days))), 2: np.round(m * .4)}, index=days)


def test_viirs_outage_is_found_and_quiet_days_are_not():
    d = world(pd.date_range(VIIRS_START, periods=200))
    d.loc["2012-05-01":"2012-05-03", 1] = 40      # VIIRS out
    d.loc["2012-06-10", [0, 1, 2]] = [400, 1000, 160]  # a quiet day for every sensor: not an outage
    assert prior.viirs_gaps(d) == ["2012-05-01", "2012-05-02", "2012-05-03"]


def test_lost_orbit_strip_is_found_in_its_band_only():
    days = pd.date_range(VIIRS_START, periods=120)
    rows = [(day, b, s, n) for day in days for b in (0, 1) for s, n in ((0, 300), (1, 780))]
    bands = pd.DataFrame(rows, columns=["d", "b", "s", "n"])
    bands.loc[(bands.d == "2012-03-01") & (bands.b == 1) & (bands.s == 1), "n"] = 5  # VIIRS lost band 1 that day
    bands.loc[(bands.d == "2012-03-02") & (bands.b == 0) & (bands.s == 1), "n"] = 400  # a cloudy day: half, not out
    assert prior.viirs_strips(bands, gaps=[]) == {"2012-03-01": [1]}
    assert prior.viirs_strips(bands, gaps=["2012-03-01"]) == {}  # already a worldwide outage


def test_modis_terra_and_aqua_outages():
    d = world(pd.date_range("2001-01-01", "2004-12-31"))
    d.loc[: AQUA_START - pd.Timedelta(days=1), 2] = d.loc[: AQUA_START - pd.Timedelta(days=1), 0]  # Terra only before Aqua
    d.loc["2001-06-16":"2001-07-02", [0, 2]] = 0       # MODIS (Terra) out before Aqua
    d.loc["2003-12-17":"2003-12-24", 2] = 0            # Terra out: MODIS holds Aqua alone
    d.loc["2002-07-30":"2002-08-07", 0] = d.loc["2002-07-30":"2002-08-07", 2]  # Aqua out: MODIS = Terra
    out, aqua = prior.modis_gaps(d)
    assert out == [x.date().isoformat() for x in pd.date_range("2001-06-16", "2001-07-02")] + \
                  [x.date().isoformat() for x in pd.date_range("2003-12-17", "2003-12-24")]
    assert aqua == [x.date().isoformat() for x in pd.date_range("2002-07-30", "2002-08-07")]
