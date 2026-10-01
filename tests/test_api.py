"""API contract against the real processed data (skips if none is present)."""
import pytest
from fastapi.testclient import TestClient

from app.main import app, store

pytestmark = pytest.mark.skipif(not store.ready, reason="no processed countries")
client = TestClient(app)


def test_health_and_meta():
    assert client.get("/api/health").json()["ok"]
    meta = client.get("/api/meta").json()
    assert len(meta["countries"]) > 200


def test_calendar_for_ready_country():
    cid = store.ready[0]
    r = client.get(f"/api/calendar?country={cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["harmonization"]["k_all"] > 0
    assert len(body["monthly"]["values"][0]) == 12


def test_bad_inputs_are_rejected():
    assert client.get("/api/calendar").status_code == 400
    assert client.get("/api/calendar?bbox=1,2").status_code == 400
    assert client.get("/api/calendar?bbox=10,10,5,5").status_code == 400
    assert client.get("/api/calendar?country=Atlantis").status_code == 404
    assert client.get("/api/calendar?bbox=-170,-80,170,80").status_code == 400  # too large
    assert client.post("/api/prepare", json=["Atlantis"]).status_code == 404


def test_grid_and_locate():
    assert "cells" in client.get("/api/grid?bbox=-180,-60,180,75").json()
    assert client.get("/api/locate?lon=33.4&lat=35.1").json()["country"] == "Cyprus"


def test_index_is_versioned():
    html = client.get("/").text
    assert "{{v}}" not in html and "app.js?v=" in html
