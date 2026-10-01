"""Background queue that downloads + grids a country's FIRMS archive on demand.

One worker processes countries in order, so a burst of requests can't saturate the
network or disk. Raw CSVs are deleted after gridding (the whole world is ~30 GB raw,
~1 GB gridded).
"""
from __future__ import annotations

import queue
import threading
import time
import traceback

from pipeline.build import build_country
from pipeline.fetch import fetch_country


class Jobs:
    def __init__(self, on_ready, keep_raw: bool = False):
        self._q: queue.Queue[str] = queue.Queue()
        self._state: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._on_ready = on_ready
        self._keep_raw = keep_raw
        threading.Thread(target=self._worker, daemon=True, name="country-jobs").start()

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
        return self.status(cid)

    def _worker(self):
        while True:
            cid = self._q.get()
            try:
                self._set(cid, state="downloading", message="Downloading NASA FIRMS archives")
                fetch_country(cid, progress=lambda i, n, m: self._set(cid, done=i, total=n, message=m))
                self._set(cid, state="building", message="Cleaning and gridding hotspots")
                build_country(cid, keep_raw=self._keep_raw)
                self._on_ready(cid)
                self._set(cid, state="ready", message="Ready")
            except Exception as e:
                traceback.print_exc()
                self._set(cid, state="error", message=str(e), error=str(e))
            finally:
                self._q.task_done()
