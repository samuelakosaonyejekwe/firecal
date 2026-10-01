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
