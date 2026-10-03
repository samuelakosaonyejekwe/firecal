"""Background queue that brings a country's harmonized record onto this computer on request.

One worker processes countries in order, so a burst of requests can't saturate the network or
disk. Each country comes from the cheapest source (pipeline/sync.py): GitHub if it is already
published there (a download of at most ~50 MB), otherwise NASA FIRMS (downloaded, gridded, raw files
deleted) and then, with a gh login that may write to the repository, published to GitHub so the
website and repository stay in step with this computer. When the queue empties after publishing,
GitHub is asked to rebuild the website.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback

from pipeline.sync import open_store, sync_country


class Jobs:
    def __init__(self, on_ready):
        self._q: queue.Queue[str] = queue.Queue()
        self._state: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._on_ready = on_ready
        self._thread: threading.Thread | None = None
        self._published = 0
        self._store, self._store_at = None, 0.0

    STORE_AGE = 600  # seconds before the release's file list is read again (GitHub limits anonymous reads to 60/hour)

    def _get_store(self):
        """The release to sync with, re-read every STORE_AGE seconds; a failed re-read keeps the last good one."""
        if self._store is None or time.time() - self._store_at > self.STORE_AGE:
            try:
                fresh = open_store()
                if fresh is not None or self._store is None:
                    self._store, self._store_at = fresh, time.time()
            except Exception:
                if self._store is None:
                    raise
        return self._store

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
        with self._lock:  # check and enqueue in one step: two requests at once queue a country only once
            st = self._state.get(cid, {}).get("state")
            fresh = st not in ("queued", "downloading", "building")
            if fresh:
                self._state[cid] = {"state": "queued", "done": 0, "total": 0, "message": "Waiting in queue",
                                    "error": None, "t": time.time()}
                self._q.put(cid)
        self._ensure_worker()  # also restarts a worker that died with countries still queued
        return self.status(cid)

    def _worker(self):
        store = None
        while True:
            cid = self._q.get()
            try:
                self._set(cid, state="downloading", message="Fetching the fire record")
                store = self._get_store()
                how, changed = sync_country(cid, store, progress=lambda i, n, m, cid=cid: self._set(
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
            if store and store.can_write and self._published and self._q.empty():
                try:
                    store.rebuild_site()
                except Exception:  # the website catches up on its next scheduled build
                    traceback.print_exc()
                self._published = 0
