"""app/feeds.py: the newest NASA data is found on either server; re-stamped unchanged files don't count."""
import datetime as dt

import pytest

import app.feeds as feeds

T = dt.datetime(2026, 10, 1, 21, 54, tzinfo=dt.timezone.utc)
MAIN, MIRROR = feeds.FEEDS


def servers(monkeypatch, main, mirror):
    monkeypatch.setattr(feeds, "head", lambda url, timeout=60: main if url == MAIN else mirror)


def test_restamped_unchanged_file_keeps_its_first_time(monkeypatch):
    servers(monkeypatch, (T, 1000), (T + dt.timedelta(minutes=51), 1000))  # as seen on 1 Oct 2026
    url, first, size = feeds.newest()
    assert first == T and size == 1000


def test_new_data_on_either_server_is_picked_up(monkeypatch):
    servers(monkeypatch, (T, 1000), (T + dt.timedelta(minutes=40), 1200))
    assert feeds.newest() == (MIRROR, T + dt.timedelta(minutes=40), 1200)
    servers(monkeypatch, (T + dt.timedelta(minutes=40), 1300), (T, 1000))
    assert feeds.newest() == (MAIN, T + dt.timedelta(minutes=40), 1300)


def test_one_server_down_uses_the_other(monkeypatch):
    servers(monkeypatch, None, (T, 1000))
    assert feeds.newest() == (MIRROR, T, 1000)
    servers(monkeypatch, None, None)
    with pytest.raises(OSError):
        feeds.newest(waits=())


def test_new_data_beats_an_old_file_restamped_later(monkeypatch):
    # main re-stamps the old file (size 1000) at 22:20 while the mirror already has new data since 22:05
    servers(monkeypatch, (T + dt.timedelta(minutes=26), 1000), (T + dt.timedelta(minutes=11), 1200))
    assert feeds.newest(known=(1000, T)) == (MIRROR, T + dt.timedelta(minutes=11), 1200)
    # with nothing known yet, the latest stamp still wins
    assert feeds.newest()[0] == MAIN


def test_a_lagging_servers_older_file_is_never_preferred(monkeypatch):
    # we have the new 2000-byte data (10:00); the mirror still shows the old 1000-byte file (09:00)
    servers(monkeypatch, (T + dt.timedelta(hours=1), 2000), (T, 1000))
    assert feeds.newest(known=(2000, T + dt.timedelta(hours=1)))[2] == 2000  # no flip back to the older file


def test_an_older_file_never_rolls_the_caller_back(monkeypatch):
    # the caller has the 22:30 data (size 2000); the main server is down and the mirror still serves 22:00's (1000)
    servers(monkeypatch, None, (T, 1000))
    assert feeds.newest(known=(2000, T + dt.timedelta(minutes=30)))[1:] == (T + dt.timedelta(minutes=30), 2000)
