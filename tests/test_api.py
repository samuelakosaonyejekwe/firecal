"""API contract on synthetic data (see conftest.py): runs everywhere, touches no network."""
from fastapi.testclient import TestClient

from app.main import app, store

client = TestClient(app)


def test_health_and_meta():
    assert client.get("/api/health").json()["ok"]
    meta = client.get("/api/meta").json()
    assert len(meta["countries"]) > 200
    assert [c["id"] for c in meta["countries"] if c["ready"]] == ["Maldives", "Nigeria"]
    assert set(meta["prior"]) == {"k_world", "k_terra_world"}


def test_calendar_for_ready_country():
    r = client.get("/api/calendar?country=Nigeria")
    assert r.status_code == 200
    body = r.json()
    assert abs(body["harmonization"]["k_all"] - 3.0) < 0.1
    assert len(body["monthly"]["values"][0]) == 12


def test_country_without_any_fires_gets_an_empty_calendar():
    r = client.get("/api/calendar?country=Maldives")
    assert r.status_code == 200
    body = r.json()
    assert body["total_cell_days"] == 0 and body["harmonization"]["low_counts"]


def test_unprocessed_country_and_ocean_box_are_answers_not_errors():
    r = client.get("/api/calendar?country=Ghana")
    assert r.status_code == 202 and r.json()["needs_data"] and r.json()["missing"][0]["id"] == "Ghana"
    r = client.get("/api/calendar?bbox=-30,-30,-25,-25")
    assert r.status_code == 202 and r.json()["missing"] == []


def test_bad_inputs_are_rejected():
    assert client.get("/api/calendar").status_code == 400
    assert client.get("/api/calendar?bbox=1,2").status_code == 400
    assert client.get("/api/calendar?bbox=10,10,5,5").status_code == 400
    assert client.get("/api/calendar?country=Atlantis").status_code == 404
    assert client.get("/api/calendar?bbox=-170,-80,170,80").status_code == 400  # too large
    assert client.post("/api/prepare", json=["Atlantis"]).status_code == 404
    assert client.post("/api/prepare", json=["Chad"] * 13).status_code == 400
    assert client.get("/api/grid?bbox=0,0,1,1&month=13").status_code == 422


def test_grid_and_locate():
    g = client.get("/api/grid?bbox=-180,-60,180,75").json()
    assert g["cells"] and g["countries"] == ["Maldives", "Nigeria"]
    assert client.get("/api/locate?lon=33.4&lat=35.1").json()["country"] == "Cyprus"
    assert client.get("/api/locate?lon=-30&lat=-30").json()["country"] is None


def test_live_endpoints_without_feed():
    assert client.get("/api/live").status_code == 503  # no live feed downloaded in tests
    assert client.get("/api/nowcast?country=Nigeria").json()["available"] is False


def test_index_is_versioned_and_assets_cacheable():
    html = client.get("/").text
    assert "{{v}}" not in html and "app.js?v=" in html and "engine.js?v=" in html
    r = client.get("/static/app.js")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert "{{v}}" not in client.get("/sw.js").text


def test_results_do_not_depend_on_other_loaded_countries(tmp_path):
    """The calibration prior is fixed, so adding an unrelated country must not move an area's numbers."""
    from app.analysis import Store
    from app.main import RES
    from tests.test_analysis import make_country
    make_country(tmp_path, "Nigeria", seed=3)
    one = Store(tmp_path, RES).analyze({"country": "Nigeria"})
    make_country(tmp_path, "Ghana", k=2.0, kt=5.0, seed=4, season=(7, 8))
    two = Store(tmp_path, RES).analyze({"country": "Nigeria"})
    assert one["harmonization"] == two["harmonization"] and one["total_cell_days"] == two["total_cell_days"]
    assert one["total_cell_days"] == store.analyze({"country": "Nigeria"})["total_cell_days"]


def test_cached_results_follow_code_changes(tmp_path, monkeypatch):
    """A change to the analysis code must not be hidden by results cached by the old code."""
    import app.analysis as A
    from app.main import RES
    from tests.test_analysis import make_country
    make_country(tmp_path, "Nigeria", seed=3)
    s = A.Store(tmp_path, RES)
    s.analyze({"country": "Nigeria"})
    monkeypatch.setattr(A, "CODE_VERSION", "different")
    monkeypatch.setattr(A.Store, "_analyze", lambda self, aoi: {"recomputed": True})
    assert s.analyze({"country": "Nigeria"}) == {"recomputed": True}
