"""pipeline/world.py: stops early when NASA can't be reached instead of failing every country."""
import sys

import pytest

import pipeline.world as world


def test_stops_early_when_nasa_is_unreachable(monkeypatch, capsys):
    tried = []

    def offline(cid, store, skip_published=False):
        tried.append(cid)
        raise RuntimeError(f"42 downloads failed, e.g. failed  modis_2001_{cid}.csv (<urlopen error [Errno 101] Network is unreachable>)")

    monkeypatch.setattr(world, "sync_country", offline)
    monkeypatch.setattr(sys, "argv", ["world.py", "--no-github", "Chad", "Mali", "Niger", "Benin", "Togo"])
    with pytest.raises(SystemExit) as e:
        world.main()
    assert e.value.code == 3 and len(tried) == world.OFFLINE_STOP
    assert "NASA unreachable" in capsys.readouterr().out


def test_other_failures_do_not_stop_the_run(monkeypatch):
    tried = []

    def broken(cid, store, skip_published=False):
        tried.append(cid)
        raise RuntimeError("FIRMS has no MODIS archive for this territory")

    monkeypatch.setattr(world, "sync_country", broken)
    monkeypatch.setattr(sys, "argv", ["world.py", "--no-github", "Chad", "Mali", "Niger", "Benin", "Togo"])
    world.main()
    assert len(tried) == 5
