"""Keep the website's live fires fresh even when GitHub's scheduled runs are delayed or dropped.

GitHub runs scheduled workflows on a best-effort basis (they are delayed or skipped when Actions is
busy or degraded), so the website does not rely on them. A keeper checks whether NASA has published
live data that the website doesn't show yet; if the website is more than `grace` minutes behind and no
website build is already queued or running, it asks GitHub to rebuild the site. It does nothing when
the site is current. It runs in two places:

  * in the cloud, around the clock: .github/workflows/live.yml runs `keeper.py --watch`, checking
    every 5 minutes for ~5.5 hours, then starts its own successor (no laptop, no GitHub schedule);
  * on this computer, every 15 minutes while the local FireCal server runs (a second safety net).

Each check also restarts the cloud watcher if none is running, and switches the website schedule
back on if GitHub has disabled it (GitHub does that after 60 days without repository activity).

    .venv/bin/python pipeline/keeper.py              # one check
    .venv/bin/python pipeline/keeper.py --watch 320  # check every 5 minutes for 320 minutes (cloud)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import threading
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.feeds import newest  # noqa: E402
from pipeline.sync import WORKFLOW, gh, github_available  # noqa: E402

WATCHER = "live.yml" # the cloud watcher workflow
INTERVAL = 15 * 60   # seconds between checks on this computer
WATCH_EVERY = 5 * 60 # seconds between checks in the cloud watcher
GRACE = 20           # minutes to leave GitHub's own schedule before stepping in (this computer)
WATCH_GRACE = 5      # the same, for the cloud watcher
COOLDOWN = 30        # minutes between rebuild requests from the keeper
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")


def nasa_updated() -> dt.datetime:
    """When NASA first published its current data (either server; re-stamps of the same file don't count)."""
    return newest()[1]


def website_url() -> str:
    if os.environ.get("FIRECAL_SITE_URL"):  # set by the cloud watcher (its key can't read Pages settings)
        return os.environ["FIRECAL_SITE_URL"].rstrip("/") + "/"
    return json.loads(gh("api", "repos/{owner}/{repo}/pages"))["html_url"].rstrip("/") + "/"


def website_updated(url: str) -> dt.datetime | None:
    try:
        with urllib.request.urlopen(url + "data/live/meta.json?nocache=" + str(int(time.time())), timeout=60) as r:
            return dt.datetime.fromisoformat(json.load(r)["source_last_modified"])
    except Exception:
        return None


def build_in_flight(workflow: str = WORKFLOW) -> bool:
    runs = json.loads(gh("run", "list", "--workflow", workflow, "--limit", "5", "--json", "status"))
    return any(r["status"] in ACTIVE for r in runs)


def ensure_watcher() -> str | None:
    """Start the cloud watcher if none is running or queued (it normally hands over to itself)."""
    if os.environ.get("FIRECAL_WATCHER") == "1" or build_in_flight(WATCHER):
        return None
    gh("workflow", "run", WATCHER)
    return "cloud watcher was not running; started it"


def ensure_schedule_enabled() -> str | None:
    """GitHub disables scheduled workflows after 60 days without repository activity; switch it back on."""
    state = json.loads(gh("api", f"repos/{{owner}}/{{repo}}/actions/workflows/{WORKFLOW}"))["state"]
    if state != "active":
        gh("workflow", "enable", WORKFLOW)
        return f"website schedule was {state}; re-enabled it"
    return None


def check_once(last_request: dt.datetime | None = None, grace: int = GRACE) -> tuple[str, dt.datetime | None]:
    """One check. Returns (what happened, time of the last rebuild request)."""
    notes = []
    for fix in (ensure_schedule_enabled, ensure_watcher):
        try:
            if note := fix():
                notes.append(note)
        except Exception as e:  # never let a side check stop the freshness check
            notes.append(f"{fix.__name__} skipped ({e.__class__.__name__})")
    msg, last = _check_freshness(last_request, grace)
    return "; ".join(notes + [msg]), last


def _check_freshness(last_request: dt.datetime | None, grace: int = GRACE) -> tuple[str, dt.datetime | None]:
    now = dt.datetime.now(dt.timezone.utc)
    nasa = nasa_updated()
    site = website_updated(website_url())
    if site and site >= nasa:
        return f"website is current (NASA {nasa:%H:%M} UTC)", last_request
    if now - nasa < dt.timedelta(minutes=grace):
        return f"NASA updated at {nasa:%H:%M} UTC; giving GitHub's schedule until {nasa + dt.timedelta(minutes=grace):%H:%M}", last_request
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


def watch(minutes: float, log=print):
    """The cloud watcher: check every WATCH_EVERY seconds for `minutes`, then return (the workflow hands over)."""
    os.environ["FIRECAL_WATCHER"] = "1"  # this process is the watcher; don't start another
    end, last = time.time() + minutes * 60, None
    while True:
        try:
            msg, last = check_once(last, grace=WATCH_GRACE)
            log(f"{dt.datetime.now(dt.timezone.utc):%H:%M} {msg}")
        except Exception as e:  # NASA or GitHub briefly unreachable: try again next time
            log(f"{dt.datetime.now(dt.timezone.utc):%H:%M} check skipped ({e.__class__.__name__}: {e})")
        if time.time() + WATCH_EVERY > end:
            return
        time.sleep(WATCH_EVERY)


if __name__ == "__main__":
    if sys.argv[1:2] in (["-h"], ["--help"]):
        print(__doc__)
    elif sys.argv[1:2] == ["--watch"]:
        watch(float(sys.argv[2]), log=lambda m: print(m, flush=True))
    else:
        print(check_once()[0])
