"""Single source of truth for FireCal's fixed parameters.

Every Python module imports from here. The browser engine (app/static/engine.js) and data
layer (app/static/data.js) carry JavaScript copies; tests/test_engine_parity.py checks those
copies against these values, and the static site writes the tile sizes into its index files.
"""
import json
import pathlib

import pandas as pd

CELL = 10  # grid cells per degree -> 0.1° cells

# sensor record (UTC dates)
MODIS_START = pd.Timestamp("2000-11-01")  # first month of the Terra MODIS fire archive
AQUA_START = pd.Timestamp("2002-07-04")   # first Aqua MODIS fire product day
VIIRS_START = pd.Timestamp("2012-01-20")  # first day of the S-NPP VIIRS 375 m archive
# Terra left the morning constellation in Oct 2022 and its overpass is drifting earlier,
# so its detection rate falls; the drifted years stay out of the Terra-only calibration.
TERRA_DRIFT = pd.Timestamp("2023-01-01")

# harmonization shrinkage (pseudo-counts, in MODIS cell-days)
LAMBDA = 20.0        # month ratio -> area ratio
LAMBDA_AREA = 50.0   # area ratio -> worldwide ratio

# largest custom box (square degrees); whole countries have no limit
SERVER_MAX_BOX_DEG2 = 5000   # ≈ 70° × 70°, analysed on the server
WEB_MAX_BOX_DEG2 = 400       # 20° × 20°, analysed in the visitor's browser from downloaded tiles

# static-site tiling (in 0.1° cells)
BOX_TILE = 20    # 2° tiles of every fire cell-day, for drawn boxes
MAP_TILE = 100   # 10° tiles for map layers and live fires
OVERVIEW = 5     # 0.5° world overview for map layers

# NASA FIRMS near-real-time feed (VIIRS S-NPP, same sensor as the archive reference)
NRT_URL = "https://firms.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_7d.csv"

# Worldwide MODIS->VIIRS calibration prior. Fixed and versioned (app/resources/prior.json,
# written by pipeline/prior.py) so an area's results never depend on which other countries
# happen to be loaded on a given machine.
PRIOR_FILE = pathlib.Path(__file__).resolve().parent / "resources" / "prior.json"


def load_prior() -> tuple[float, float]:
    p = json.loads(PRIOR_FILE.read_text())
    return float(p["k_world"]), float(p["k_terra_world"])
