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

from .test_analysis import RES, make_country

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGINE = ROOT / "app" / "static" / "engine.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

RUNNER = """
const E = require(process.argv[1]);
const inp = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const S = E.series(inp.endYear);
const nz = (a) => a.map((x) => (x === null ? NaN : x));
S.m.set(inp.m); S.t.set(inp.t); S.v.set(nz(inp.v));
process.stdout.write(JSON.stringify(E.analyze(S, inp.prior, { aoi: inp.aoi, label: 'x' })));
"""


def run_js(store: Store, aoi):
    day, *_ = store.series(aoi)
    pm, pt = store.prior()
    gaps = [d.date().isoformat() for d in VIIRS_GAPS]
    # as on the website: the browser's tiles hold 0 on VIIRS outage days, and the prior lists those days
    v = [0.0 if d.date().isoformat() in gaps else (None if math.isnan(x) else x) for d, x in zip(day.index, day.v)]
    inp = {"endYear": store.end.year, "aoi": aoi, "prior": {"k_world": pm, "k_terra_world": pt, "viirs_gaps": gaps},
           "m": day.m.tolist(), "t": day.t.tolist(), "v": v}
    res = subprocess.run(["node", "-e", RUNNER, str(ENGINE)], input=json.dumps(inp), capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def close(a, b, tol=0.011, rel=1e-6):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) and math.isnan(a):
        return b is None or (isinstance(b, float) and math.isnan(b))
    return abs(a - b) <= tol + rel * abs(b)


def assert_lists(a, b, tol=0.011):
    assert len(a) == len(b)
    bad = [(i, x, y) for i, (x, y) in enumerate(zip(a, b)) if not close(x, y, tol)]
    assert not bad, bad[:5]


def compare(py, js):
    hp, hj = py["harmonization"], js["harmonization"]
    for k in ("k_all", "k_terra_all", "r2_monthly", "cv_median_ape", "cv_median_ape_terra", "terra_check_ape", "overlap_viirs_cell_days"):
        assert close(hp[k], hj[k], 0.0011 if k.startswith("k") else 0.11), (k, hp[k], hj[k])
    assert hp["low_counts"] == hj["low_counts"]
    assert hp["viirs_gaps"] == hj["viirs_gaps"]
    assert_lists(hp["k_month"], hj["k_month"], 0.0011)
    assert [c["year"] for c in hp["cv"]] == [c["year"] for c in hj["cv"]]
    assert close(py["total_cell_days"], js["total_cell_days"], 1.01)
    for a, b in zip(py["monthly"]["values"], js["monthly"]["values"]):
        assert_lists(a, b, 0.11)
    for a, b in zip(py["monthly"]["z"], js["monthly"]["z"]):
        assert_lists(a, b, 0.011)
    assert py["season_start_month"] == js["season_start_month"]
    assert [(s["label"], s.get("start"), s.get("peak"), s.get("end")) for s in py["seasons"]] == \
           [(s["label"], s.get("start"), s.get("peak"), s.get("end")) for s in js["seasons"]]
    assert_lists([s["total"] for s in py["seasons"]], [s["total"] for s in js["seasons"]], 0.11)
    assert py["critical"] == js["critical"]
    assert py["top_weeks"] == js["top_weeks"]
    assert [(u["year"], u["month"]) for u in py["unusual"]] == [(u["year"], u["month"]) for u in js["unusual"]]
    for k in ("m", "v", "h"):
        assert_lists(py["yearly"][k], js["yearly"][k], 1.01)
    assert py["climatology"]["keys"] == js["climatology"]["keys"]
    for k in ("mean", "p10", "p50", "p90"):
        assert_lists(py["climatology"][k], js["climatology"][k], 0.011)
    assert_lists(py["daily"]["h"][::7], js["daily"]["h"][::7], 0.011)


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
