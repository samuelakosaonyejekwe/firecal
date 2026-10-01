"""Background queue that brings a country's harmonized record onto this computer on request.

One worker processes countries in order, so a burst of requests can't saturate the network or
disk. Each country comes from the cheapest source (pipeline/sync.py): GitHub if it is already
published there (a 0.01–50 MB download), otherwise NASA FIRMS (downloaded, gridded, raw files deleted)
and then published to GitHub, so the website and repository stay in step with this computer.
When the queue empties after publishing, GitHub is asked to rebuild the website.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback

from pipeline.sync import GitHubStore, github_available, sync_country


class Jobs:
    def __init__(self, on_ready):
        self._q: queue.Queue[str] = queue.Queue()
        self._state: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._on_ready = on_ready
        self._thread: threading.Thread | None = None
        self._published = 0

    def _ensure_worker(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, daemon=True, name="country-jobs")
                self._thread.start()

    def status(self, cid=None):
        with self._lock:
            if cid:
                return dict(self._state.get(cid, {"state": "none"}))
            return {k: dict(v) for k, v in self._state.items() if v["state"] != "ready" or time.time() - v["t"] < 600}

    def _set(self, cid, **kw):
        with self._lock:
            self._state.setdefault(cid, {}).update(kw, t=time.time())

    def submit(self, cid) -> dict:
        with self._lock:
            st = self._state.get(cid, {}).get("state")
            if st in ("queued", "downloading", "building"):
                return dict(self._state[cid])
        self._set(cid, state="queued", done=0, total=0, message="Waiting in queue", error=None)
        self._q.put(cid)
        self._ensure_worker()
        return self.status(cid)

    def _worker(self):
        store = GitHubStore() if github_available() else None
        while True:
            cid = self._q.get()
            try:
                self._set(cid, state="downloading", message="Fetching the fire record")
                how, changed = sync_country(cid, store, progress=lambda i, n, m: self._set(
                    cid, state="building" if i >= n else "downloading", done=i, total=n,
                    message="Cleaning and gridding hotspots" if i >= n else m))
                self._published += changed
                self._on_ready(cid)
                self._set(cid, state="ready", message=f"Ready ({how})")
            except Exception as e:
                traceback.print_exc()
                self._set(cid, state="error", message=str(e), error=str(e))
            finally:
                self._q.task_done()
            if store and self._published and self._q.empty():
                store.rebuild_site()
                self._published = 0
