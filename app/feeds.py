"""NASA FIRMS near-real-time feed: which server has the newest data, and since when.

NASA serves the same 7-day VIIRS S-NPP file from two servers (firms and its mirror firms2). They are
updated independently, and a server sometimes re-stamps an unchanged file with a later time. So the
data's version is identified by its content (the file size, which changes with every update) and
dated by the first moment any server published that content:

  * the newest server is the one with the latest Last-Modified time, except that a caller who says which
    content it already has (`known`: its size and publication time) gets a server offering different content
    published after that: an old file re-stamped later on one server must not hide new data on the other;
  * if no server offers newer content than the caller's, the caller's data is the answer (never an older file
    still served by a lagging server while the up-to-date one is down);
  * if the other server has a file of the same size, it is the same data, published at the earlier time.

A re-stamped, unchanged file therefore never looks new (no needless website rebuilds or browser
downloads), and genuinely new data is picked up from whichever server has it first. If one server is
down, the other is used. Standard library only: the website workflow runs it before installing anything.

    python3 app/feeds.py                    # prints: <version time> <size> <url>
    python3 app/feeds.py <size> <time>      # the same, knowing which data the caller already has
"""
from __future__ import annotations

import datetime as dt
import email.utils
import pathlib
import shutil
import time
import urllib.request

PATH = "/data/active_fire/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_7d.csv"
FEEDS = ("https://firms.modaps.eosdis.nasa.gov" + PATH,   # main server
         "https://firms2.modaps.eosdis.nasa.gov" + PATH)  # mirror (also readable from browsers)


def head(url: str, timeout: int = 60) -> tuple[dt.datetime, int] | None:
    """(Last-Modified, size) of one server's copy, or None if it can't be reached."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=timeout) as r:
            return email.utils.parsedate_to_datetime(r.headers["Last-Modified"]), int(r.headers["Content-Length"])
    except Exception:
        return None


def download(url: str, dest: pathlib.Path, timeout: int = 120):
    """Save `url` to `dest`; a connection that stalls for `timeout` seconds fails instead of hanging forever."""
    with urllib.request.urlopen(url, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


def newest(timeout: int = 60, waits=(10, 30), known: tuple[int, dt.datetime] | None = None) -> tuple[str, dt.datetime, int]:
    """(url to download, when this data was first published, its size). Retries after `waits` seconds;
    raises if no server answers at all. `known`: (size, publication time) of the data the caller already has."""
    for wait in (*waits, None):
        seen = [(url, *h) for url in FEEDS if (h := head(url, timeout))]
        if seen or wait is None:
            break
        time.sleep(wait)  # NASA briefly unreachable: try again
    if not seen:
        raise OSError("no NASA FIRMS server answered")
    # content the caller doesn't have yet and that is newer than what it has (a lagging server's older file isn't)
    new = [s for s in seen if known is None or (s[2] != known[0] and s[1] > known[1])]
    if known is not None and not new:
        # nothing newer anywhere: the caller's data stands, even if the server holding it is down and the other
        # still serves an older file (which must never roll the caller back)
        same = [s for s in seen if s[2] == known[0]]
        return (same or seen)[0][0], min([known[1], *(m for _, m, _ in same)]), known[0]
    url, _, size = max(new or seen, key=lambda s: s[1])
    first = min(m for _, m, n in seen if n == size)  # same content elsewhere: it was published then
    return url, first, size


if __name__ == "__main__":
    import sys
    known = (int(sys.argv[1]), dt.datetime.fromisoformat(sys.argv[2])) if len(sys.argv) == 3 else None
    u, m, n = newest(known=known)
    print(m.isoformat(), n, u)
