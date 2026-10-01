"""Keep the website's live fires fresh even when GitHub's scheduled runs are delayed or dropped.

GitHub runs scheduled workflows on a best-effort basis (they are delayed or skipped when Actions is
busy or degraded). While the local FireCal server is running, this keeper checks every 15 minutes
whether NASA has published live data that the website doesn't show yet. If the website is more than
GRACE minutes behind and no website build is already queued or running, it asks GitHub to rebuild
the site, with your own `gh` login. It does nothing when the site is current.

Run once by hand:  .venv/bin/python pipeline/keeper.py
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import os
import pathlib
import sys
import threading
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.constants import NRT_URL  # noqa: E402
from pipeline.sync import WORKFLOW, gh, github_available  # noqa: E402

INTERVAL = 15 * 60   # seconds between checks
GRACE = 20           # minutes to leave GitHub's own schedule before stepping in
COOLDOWN = 30        # minutes between rebuild requests from the keeper


def nasa_updated() -> dt.datetime:
    with urllib.request.urlopen(urllib.request.Request(NRT_URL, method="HEAD"), timeout=60) as r:
        return email.utils.parsedate_to_datetime(r.headers["Last-Modified"])


def website_url() -> str:
    return json.loads(gh("api", "repos/{owner}/{repo}/pages"))["html_url"].rstrip("/") + "/"


def website_updated(url: str) -> dt.datetime | None:
    try:
        with urllib.request.urlopen(url + "data/live/meta.json?nocache=" + str(int(time.time())), timeout=60) as r:
            return dt.datetime.fromisoformat(json.load(r)["source_last_modified"])
    except Exception:
        return None


def build_in_flight() -> bool:
    runs = json.loads(gh("run", "list", "--workflow", WORKFLOW, "--limit", "5", "--json", "status"))
    return any(r["status"] in ("queued", "in_progress", "waiting", "pending", "requested") for r in runs)


def check_once(last_request: dt.datetime | None = None) -> tuple[str, dt.datetime | None]:
    """One check. Returns (what happened, time of the last rebuild request)."""
    now = dt.datetime.now(dt.timezone.utc)
    nasa = nasa_updated()
    site = website_updated(website_url())
    if site and site >= nasa:
        return f"website is current (NASA {nasa:%H:%M} UTC)", last_request
    if now - nasa < dt.timedelta(minutes=GRACE):
        return f"NASA updated at {nasa:%H:%M} UTC; giving GitHub's schedule until {nasa + dt.timedelta(minutes=GRACE):%H:%M}", last_request
    if build_in_flight():
        return "a website build is already queued or running", last_request
    if last_request and now - last_request < dt.timedelta(minutes=COOLDOWN):
        return "rebuild already requested recently; waiting", last_request
    gh("workflow", "run", WORKFLOW)
    why = f"website behind NASA ({site:%H:%M} vs {nasa:%H:%M} UTC)" if site else "website live data unreadable"
    return f"{why}; asked GitHub to rebuild", now


def start(log=print):
    """Background keeper for the local server (no-op without a GitHub login for this repository)."""
    if os.environ.get("FIRECAL_NO_KEEPER") == "1" or not github_available():
        return False

    def loop():
        last = None
        while True:
            try:
                msg, last = check_once(last)
                log(f"freshness keeper: {msg}")
            except Exception as e:  # network down, GitHub unavailable: try again next time
                log(f"freshness keeper: check skipped ({e.__class__.__name__}: {e})")
            time.sleep(INTERVAL)
    threading.Thread(target=loop, daemon=True, name="website-keeper").start()
    return True


if __name__ == "__main__":
    print(check_once()[0])
