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


def test_dropped_connection_to_nasa_is_retried(monkeypatch):
    import contextlib
    import urllib.error
    import pipeline.fetch as fetch
    calls = []

    def flaky(req, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError(OSError(101, "Network is unreachable"))
        return contextlib.nullcontext()
    monkeypatch.setattr(fetch.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    assert fetch.published("modis", 2024, "Brazil") is True and len(calls) == 3


def test_update_stops_cleanly_when_nasa_is_unreachable(monkeypatch, capsys):
    def down():
        raise OSError(101, "Network is unreachable")
    monkeypatch.setattr(world, "latest_archive_year", down)
    monkeypatch.setattr(sys, "argv", ["world.py", "--no-github", "--update", "Chad"])
    with pytest.raises(SystemExit) as e:
        world.main()
    assert e.value.code == 3 and "NASA unreachable" in capsys.readouterr().out


def test_time_budget_stops_starting_new_countries(monkeypatch, capsys):
    clock, tried = [0.0], []

    def slow(cid, store, skip_published=False):
        tried.append(cid)
        clock[0] += 50 * 60  # each country takes 50 minutes
        return "built", False
    monkeypatch.setattr(world, "sync_country", slow)
    monkeypatch.setattr(world.time, "time", lambda: clock[0])
    monkeypatch.setattr(sys, "argv", ["world.py", "--no-github", "--minutes", "120", "Chad", "Mali", "Niger", "Benin"])
    world.main()
    assert tried == ["Chad", "Benin", "Mali"]  # (priority first, then A-Z) Mali starts at 100 min; Niger would at 150 (> 120)
    assert "1 countries left for the next attempt" in capsys.readouterr().out


def test_missing_with_update_lists_countries_behind_nasa(monkeypatch, tmp_path):
    import json

    class Store:
        def unavailable(self): return {}
        def has(self, cid): return cid in ("Chad", "Mali")

    def fake_gh(*args):
        d = args[args.index("--dir") + 1]
        for cid, year in (("Chad", 2024), ("Mali", 2023)):
            open(f"{d}/{cid}.built.json", "w").write(json.dumps({"archive_through": year}))
    monkeypatch.setattr(world, "GitHubStore", Store)
    monkeypatch.setattr(world, "gh", fake_gh)
    monkeypatch.setattr(world, "latest_archive_year", lambda: 2024)
    got = world.missing(update=True)
    assert "Mali" in got and "Chad" not in got  # Mali was built with 2023; Chad is current
    assert "Niger" in got  # never published
