"""NASA FIRMS near-real-time feed: which server has the newest data, and since when.

NASA serves the same 7-day VIIRS S-NPP file from two servers (firms and its mirror firms2). They are
updated independently, and a server sometimes re-stamps an unchanged file with a later time. So the
data's version is identified by its content (the file size, which changes with every update) and
dated by the first moment any server published that content:

  * the newest server is the one with the latest Last-Modified time;
  * if the other server has a file of the same size, it is the same data, published at the earlier time.

A re-stamped, unchanged file therefore never looks new (no needless website rebuilds or browser
downloads), and genuinely new data is picked up from whichever server has it first. If one server is
down, the other is used. Standard library only: the website workflow runs it before installing anything.

    python3 app/feeds.py    # prints: <version time> <size> <url>
"""
from __future__ import annotations

import datetime as dt
import email.utils
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


def newest(timeout: int = 60, waits=(10, 30)) -> tuple[str, dt.datetime, int]:
    """(url to download, when this data was first published, its size). Retries after `waits` seconds;
    raises if no server answers at all."""
    for wait in (*waits, None):
        seen = [(url, *h) for url in FEEDS if (h := head(url, timeout))]
        if seen or wait is None:
            break
        time.sleep(wait)  # NASA briefly unreachable: try again
    if not seen:
        raise OSError("no NASA FIRMS server answered")
    url, modified, size = max(seen, key=lambda s: s[1])
    first = min(m for _, m, n in seen if n == size)  # same content elsewhere: it was published then
    return url, first, size


if __name__ == "__main__":
    u, m, n = newest()
    print(m.isoformat(), n, u)
