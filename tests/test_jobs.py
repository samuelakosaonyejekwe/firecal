"""app/jobs.py: the on-demand queue brings each country in once, and keeps working after a failure."""
import threading
import time

import app.jobs as jobs


def test_a_country_is_queued_once_and_a_failure_does_not_stop_the_queue(monkeypatch):
    started, release = [], threading.Event()

    def fake_sync(cid, store, progress=None):
        started.append(cid)
        release.wait(5)
        if cid == "Mali":
            raise RuntimeError("NASA unreachable")
        return "downloaded from GitHub", False

    monkeypatch.setattr(jobs, "sync_country", fake_sync)
    monkeypatch.setattr(jobs, "open_store", lambda: None)  # GitHub switched off
    ready = []
    q = jobs.Jobs(on_ready=ready.append)
    for cid in ("Mali", "Mali", "Chad", "Chad"):  # double clicks
        q.submit(cid)
    release.set()
    for _ in range(100):
        if q.status("Chad")["state"] == "ready":
            break
        time.sleep(0.05)
    assert started == ["Mali", "Chad"]  # each once, in order
    assert q.status("Mali")["state"] == "error" and "NASA unreachable" in q.status("Mali")["error"]
    assert q.status("Chad")["state"] == "ready" and ready == ["Chad"]
