"""pipeline/finalize.py: the worldwide prior is recomputed once the world has grown enough, and when it is complete."""
import json

import pipeline.finalize as fin
from tests.test_analysis import make_country


def setup(tmp_path, monkeypatch, listed, available):
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps({"k_world": 2.5, "k_terra_world": 7.0, "countries": listed}))
    monkeypatch.setattr(fin, "PRIOR_FILE", prior)
    monkeypatch.setattr(fin, "DATA", tmp_path / "data")
    for i, cid in enumerate(available):
        make_country(tmp_path / "data", cid, k=3.0, kt=8.0, seed=i)
    return prior


def test_not_due_until_the_world_has_grown(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, ["Nigeria", "Ghana"], ["Nigeria", "Ghana"])
    assert "up to date" in fin.update_prior(push=False)


def test_recomputed_when_due_without_pushing(tmp_path, monkeypatch):
    names = ["Nigeria", "Ghana", "Togo", "Benin", "Niger", "Chad", "Mali"]
    prior = setup(tmp_path, monkeypatch, ["Nigeria"], names)
    assert "would recompute" in fin.update_prior(dry_run=True)
    msg = fin.update_prior(push=False)
    out = json.loads(prior.read_text())
    assert "recomputed from 7 countries" in msg and out["countries"] == sorted(names)
    assert abs(out["k_world"] - 3.0) < 0.05  # synthetic VIIRS = 3 × MODIS
    assert "up to date" in fin.update_prior(push=False)  # and then it's settled


def test_complete_world_is_recomputed_below_the_growth_step(tmp_path, monkeypatch):
    world = sorted(c["id"] for c in json.loads((fin.ROOT / "app" / "resources" / "countries.json").read_text()))
    listed = [c for c in world if c not in ("Nigeria", "Ghana", "Togo", "Benin", "Niger")][:30]
    setup(tmp_path, monkeypatch, listed, listed + ["Nigeria", "Ghana", "Togo", "Benin", "Niger"])
    assert "up to date" in fin.update_prior(dry_run=True)                 # 5 more of 30 is under 20%...
    assert "would recompute" in fin.update_prior(dry_run=True, complete=True)  # ...but the world is complete
