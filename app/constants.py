"""Single source of truth for FireCal's fixed parameters.

Every Python module imports from here. The browser keeps one JavaScript copy, in
app/static/engine.js (FireEngine.constants, which data.js and app.js read); tests/test_engine_parity.py
checks it against these values, and the static site writes the tile sizes into its index files.
"""
import json
import pathlib

import pandas as pd

CELL = 10  # grid cells per degree -> 0.1° cells

# sensor record (UTC dates)
MODIS_START = pd.Timestamp("2000-11-01")  # first month of the Terra MODIS fire archive
AQUA_START = pd.Timestamp("2002-07-04")   # first Aqua MODIS fire product day
VIIRS_START = pd.Timestamp("2012-01-20")  # first day of the S-NPP VIIRS 375 m archive
# Terra's overpass has drifted earlier since it stopped orbit corrections (it left the morning constellation in
# Oct 2022), so its detection rate falls. Worldwide, VIIRS/Terra fire cell-days were 6.73 ± 0.25 a year in
# 2012–2019 and 6.74–6.88 in 2020–2021, then 7.62 in 2022 (+3.6 σ), 7.34 in 2023 and 7.91 in 2024, while Terra's
# share of MODIS fell from ~0.39 to 0.37: from 2022 on the drifted years stay out of the Terra-only calibration.
TERRA_DRIFT = pd.Timestamp("2022-01-01")

# harmonization shrinkage (pseudo-counts, in MODIS cell-days)
LAMBDA = 20.0        # month ratio -> area ratio
LAMBDA_AREA = 50.0   # area ratio -> worldwide ratio

# an area's calibration is flagged "indicative only" when any of these fails
MIN_OVERLAP_CELL_DAYS = 3000  # VIIRS fire cell-days in the 2012+ overlap years
MIN_R2 = 0.5                  # fit of harmonized MODIS to VIIRS, monthly
MAX_CV_ERROR = 15.0           # % median out-of-sample error

MAX_MAP_CELLS = 20000  # map layers are merged into coarser squares beyond this many cells

# largest custom box (square degrees); whole countries have no limit
SERVER_MAX_BOX_DEG2 = 5000   # ≈ 70° × 70°, analyzed on the server
WEB_MAX_BOX_DEG2 = 400       # 20° × 20°, analyzed in the visitor's browser from downloaded tiles

# static-site tiling (in 0.1° cells)
BOX_TILE = 20    # 2° tiles of every fire cell-day, for drawn boxes
MAP_TILE = 100   # 10° tiles for map layers and live fires
OVERVIEW = 5     # 0.5° world overview for map layers

# NASA FIRMS near-real-time feed (VIIRS S-NPP, same sensor as the archive reference): see app/feeds.py,
# which picks the server with the newest data

# Worldwide MODIS->VIIRS calibration prior. Fixed and versioned (app/resources/prior.json,
# written by pipeline/prior.py) so an area's results never depend on which other countries
# happen to be loaded on a given machine.
PRIOR_FILE = pathlib.Path(__file__).resolve().parent / "resources" / "prior.json"


def load_prior() -> tuple[float, float]:
    p = json.loads(PRIOR_FILE.read_text())
    return float(p["k_world"]), float(p["k_terra_world"])


def load_viirs_gaps() -> pd.DatetimeIndex:
    """Days when S-NPP VIIRS recorded (almost) nothing worldwide while MODIS saw fires as usual: instrument
    outages, not quiet days. Detected once over every country by pipeline/prior.py and versioned with the prior."""
    return pd.DatetimeIndex(json.loads(PRIOR_FILE.read_text()).get("viirs_gaps", []))
