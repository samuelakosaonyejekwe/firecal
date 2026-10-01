"""pipeline/keeper.py: asks GitHub for a website rebuild only when the site is genuinely behind NASA."""
import datetime as dt

import pytest

import pipeline.keeper as keeper

NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def world(monkeypatch):
    state = {"nasa": NOW - dt.timedelta(hours=1), "site": NOW - dt.timedelta(hours=1), "in_flight": False, "dispatched": 0,
             "watcher": True, "watchers_started": 0}
    monkeypatch.setattr(keeper, "nasa_updated", lambda: state["nasa"])
    monkeypatch.setattr(keeper, "website_url", lambda: "https://example.github.io/firecal/")
    monkeypatch.setattr(keeper, "website_updated", lambda url: state["site"])
    monkeypatch.setattr(keeper, "build_in_flight",
                        lambda w=keeper.WORKFLOW: state["in_flight"] if w == keeper.WORKFLOW else state["watcher"])
    state["workflow_state"], state["enabled"] = "active", 0

    def fake_gh(*args):
        if args[0] == "api":
            return '{"state": "%s"}' % state["workflow_state"]
        if args[:2] == ("workflow", "enable"):
            state["enabled"] += 1
            state["workflow_state"] = "active"
            return ""
        assert args[:2] == ("workflow", "run")
        if args[2] == keeper.WATCHER:
            state["watchers_started"] += 1
            state["watcher"] = True
        else:
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


def test_schedule_disabled_by_github_is_switched_back_on(world):
    world["workflow_state"] = "disabled_inactivity"
    msg, _ = keeper.check_once()
    assert world["enabled"] == 1 and "re-enabled" in msg and "current" in msg
    msg, _ = keeper.check_once()
    assert world["enabled"] == 1 and "re-enabled" not in msg


def test_stopped_cloud_watcher_is_restarted_once(world):
    world["watcher"] = False
    msg, _ = keeper.check_once()
    assert world["watchers_started"] == 1 and "watcher" in msg and "current" in msg
    keeper.check_once()
    assert world["watchers_started"] == 1


def test_the_watcher_never_starts_another_watcher(world, monkeypatch):
    world["watcher"] = False
    monkeypatch.setenv("FIRECAL_WATCHER", "1")
    keeper.check_once()
    assert world["watchers_started"] == 0


def test_cloud_watch_rebuilds_quickly_and_ends_on_time(world, monkeypatch):
    world["nasa"], world["site"] = NOW - dt.timedelta(minutes=10), NOW - dt.timedelta(hours=3)
    monkeypatch.setattr(keeper, "WATCH_EVERY", 0)
    monkeypatch.setenv("FIRECAL_WATCHER", "0")  # watch() sets it to 1; restored after the test
    lines = []
    keeper.watch(0, log=lines.append)  # a 0-minute watch makes exactly one check
    assert len(lines) == 1 and "asked GitHub to rebuild" in lines[0] and world["dispatched"] == 1
    # (10 minutes after NASA is past the watcher's 5-minute grace, though inside the laptop's 20)


def test_site_url_from_the_environment(monkeypatch):
    monkeypatch.setenv("FIRECAL_SITE_URL", "https://someone.github.io/firecal")
    assert keeper.website_url() == "https://someone.github.io/firecal/"
