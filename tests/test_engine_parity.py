"""The browser engine (app/static/engine.js) must give the same answers as the Python engine.

Runs engine.js under Node on the exact daily series the Python Store uses, then compares every
headline output. Skips if Node isn't installed.
"""
import json
import math
import pathlib
import shutil
import subprocess

import pytest

from app.analysis import VIIRS_GAPS, Store
from pipeline.static_site import outages_for_browser

from .test_analysis import RES, make_country

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGINE = ROOT / "app" / "static" / "engine.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

RUNNER = """
const E = require(process.argv[1]);
const inp = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const S = E.series(inp.endYear);
const nz = (a) => a.map((x) => (x === null ? NaN : x));
S.m.set(inp.m); S.t.set(inp.t); S.v.set(nz(inp.v)); S.mg.set(inp.mg);
process.stdout.write(JSON.stringify(E.analyze(S, inp.prior, { aoi: inp.aoi, label: 'x' })));
"""


def run_js(store: Store, aoi):
    day, *_ = store.series(aoi)
    pm, pt = store.prior()
    gaps = {d.date().isoformat() for d in VIIRS_GAPS}
    # as on the website: the browser's tiles hold 0 on VIIRS outage days, the prior lists the outages, and data.js
    # sets the VIIRS outage strips apart (mg) as the server's SQL does
    v = [0.0 if d.date().isoformat() in gaps else (None if math.isnan(x) else x) for d, x in zip(day.index, day.v)]
    inp = {"endYear": store.end.year, "aoi": aoi, "prior": {"k_world": pm, "k_terra_world": pt, **outages_for_browser()},
           "m": day.m.tolist(), "t": day.t.tolist(), "v": v, "mg": day.mg.tolist()}
    res = subprocess.run(["node", "-e", RUNNER, str(ENGINE)], input=json.dumps(inp), capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


SKIP = {"aoi", "label", "countries", "missing", "view"}  # who asked, not what was computed
NOT_IN_TILES = {"/daily/frp"}  # the website's box tiles hold fire cells, not fire power


def compare(py, js, path=""):
    """Every computed output identical once rounded (engine.js rounds exactly as Python's round())."""
    py = json.loads(json.dumps(py))  # as served (tuples -> lists, numpy-free)
    diffs = []

    def walk(a, b, at):
        if isinstance(a, dict):
            for k in a.keys() - SKIP:
                if at + "/" + k in NOT_IN_TILES:
                    continue
                if k not in b:
                    diffs.append((at + "/" + k, "missing in engine.js"))
                else:
                    walk(a[k], b[k], at + "/" + k)
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{at}[{i}]")
        elif a != b:
            diffs.append((at, a, b))
    walk(py, js, path)
    assert not diffs, diffs[:5]


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    make_country(root, "Nigeria", seed=1)
    make_country(root, "Ghana", k=2.0, kt=5.0, seed=2, season=(7, 8))
    return Store(root, RES)


@pytest.mark.parametrize("aoi", [{"country": "Nigeria"}, {"country": "Ghana"}, {"bbox": [-4.0, 4.0, 15.0, 14.0]}])
def test_engine_matches_python_synthetic(synthetic, aoi):
    compare(synthetic.analyze(aoi), run_js(synthetic, aoi))


REAL = Store(ROOT / "data", RES) if (ROOT / "data" / "countries").exists() else None


@pytest.mark.skipif(REAL is None or not {"Nigeria", "Cyprus"} <= set(REAL.ready), reason="real data not present")
@pytest.mark.parametrize("aoi", [{"country": "Nigeria"}, {"country": "Cyprus"}, {"bbox": [5.0, 8.0, 9.0, 12.0]}])
def test_engine_matches_python_real(aoi):
    compare(REAL._analyze(aoi), run_js(REAL, aoi))


def test_engine_constants_match_python():
    """engine.js keeps JavaScript copies of the shared parameters; they must equal app/constants.py."""
    from app import constants as C
    out = subprocess.run(["node", "-e", "process.stdout.write(JSON.stringify(require(process.argv[1]).constants))", str(ENGINE)],
                         capture_output=True, text=True, check=True).stdout
    js = json.loads(out)
    ms = lambda ts: ts.value // 10**6  # noqa: E731  (pandas ns -> JS ms)
    assert js["MODIS_START"] == ms(C.MODIS_START) and js["AQUA_START"] == ms(C.AQUA_START)
    assert js["VIIRS_START"] == ms(C.VIIRS_START) and js["TERRA_DRIFT"] == ms(C.TERRA_DRIFT)
    assert js["LAMBDA"] == C.LAMBDA and js["LAMBDA_AREA"] == C.LAMBDA_AREA
    assert js["MIN_OVERLAP_CELL_DAYS"] == C.MIN_OVERLAP_CELL_DAYS and js["MIN_R2"] == C.MIN_R2
    assert js["MAX_CV_ERROR"] == C.MAX_CV_ERROR and js["MAX_MAP_CELLS"] == C.MAX_MAP_CELLS and js["CELL"] == C.CELL
    assert js["QUIET_DAYS"] == C.QUIET_DAYS
