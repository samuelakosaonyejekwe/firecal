"""pipeline/keeper.py: asks GitHub for a website rebuild only when the site is genuinely behind NASA."""
import datetime as dt

import pytest

import pipeline.keeper as keeper

NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def world(monkeypatch):
    state = {"nasa": NOW - dt.timedelta(hours=1), "site": NOW - dt.timedelta(hours=1), "in_flight": False, "dispatched": 0}
    monkeypatch.setattr(keeper, "nasa_updated", lambda: state["nasa"])
    monkeypatch.setattr(keeper, "website_url", lambda: "https://example.github.io/firecal/")
    monkeypatch.setattr(keeper, "website_updated", lambda url: state["site"])
    monkeypatch.setattr(keeper, "build_in_flight", lambda: state["in_flight"])

    def fake_gh(*args):
        assert args[:2] == ("workflow", "run")
        state["dispatched"] += 1
    monkeypatch.setattr(keeper, "gh", fake_gh)
    return state


def test_current_site_is_left_alone(world):
    msg, _ = keeper.check_once()
    assert "current" in msg and world["dispatched"] == 0


def test_github_schedule_gets_a_grace_period(world):
    world["nasa"], world["site"] = NOW - dt.timedelta(minutes=5), NOW - dt.timedelta(hours=3)
    msg, _ = keeper.check_once()
    assert "giving GitHub" in msg and world["dispatched"] == 0


def test_no_duplicate_while_a_build_is_running(world):
    world["site"], world["in_flight"] = NOW - dt.timedelta(hours=3), True
    msg, _ = keeper.check_once()
    assert "already" in msg and world["dispatched"] == 0


def test_behind_site_is_rebuilt_once_then_cools_down(world):
    world["site"] = NOW - dt.timedelta(hours=3)
    msg, last = keeper.check_once()
    assert "asked GitHub to rebuild" in msg and world["dispatched"] == 1
    msg, _ = keeper.check_once(last)
    assert "recently" in msg and world["dispatched"] == 1


def test_unreadable_site_is_rebuilt(world):
    world["site"] = None
    msg, _ = keeper.check_once()
    assert "unreadable" in msg and world["dispatched"] == 1


def test_keeper_stays_off_without_github(monkeypatch):
    monkeypatch.setenv("FIRECAL_NO_GITHUB", "1")
    assert keeper.start(log=lambda m: None) is False
