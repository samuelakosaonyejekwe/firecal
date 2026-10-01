"""Download yearly FIRMS active-fire archives (MODIS C6.1 + VIIRS S-NPP 375 m) for one country.

FIRMS publishes these per-country yearly CSVs publicly (no MAP_KEY needed):
  https://firms.modaps.eosdis.nasa.gov/data/country/{sensor}/{year}/{sensor}_{year}_{Country}.csv
Years that aren't published yet (or have no fires) return 404 and are skipped.

Usage:  python pipeline/fetch.py --country Nigeria
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import os
import pathlib
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://firms.modaps.eosdis.nasa.gov/data/country"
SENSORS = {"modis": 2000, "viirs-snpp": 2012}  # sensor -> first archive year
DATA = pathlib.Path(os.environ.get("FIRECAL_DATA", pathlib.Path(__file__).resolve().parent.parent / "data"))
RAW = DATA / "raw"
TIMEOUT = 60  # seconds without data before a download is abandoned and retried
# Never let downloads fill a disk. Under WSL, Ubuntu's disk is a file on Windows' C: drive, so C:
# is the real limit; elsewhere the data folder's own disk is.
MIN_FREE_GB = float(os.environ.get("FIRECAL_MIN_FREE_GB", "3"))
WINDOWS_C = pathlib.Path("/mnt/c")


class DiskSpaceLow(RuntimeError):
    """Raised before a download when free space is below MIN_FREE_GB (nothing is lost; resume later)."""


def free_gb() -> dict:
    disks = {"data folder": DATA if DATA.exists() else DATA.parent}
    if WINDOWS_C.is_dir():
        disks["Windows C:"] = WINDOWS_C
    return {name: shutil.disk_usage(path).free / 1e9 for name, path in disks.items()}


def ensure_space():
    low = {k: v for k, v in free_gb().items() if v < MIN_FREE_GB}
    if low:
        k, v = next(iter(low.items()))
        raise DiskSpaceLow(f"paused: only {v:.1f} GB free on {k} (FireCal keeps at least {MIN_FREE_GB:g} GB free). "
                           "Everything done so far is kept; free some space and run the same command again to resume.")


def published(sensor: str, year: int, country: str) -> bool:
    """Has FIRMS published this sensor-year archive for the country yet?"""
    name = f"{sensor}_{year}_{country}.csv"
    req = urllib.request.Request(f"{BASE}/{sensor}/{year}/{urllib.parse.quote(name)}", method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT):
            return True
    except urllib.error.HTTPError:
        return False


def download(sensor: str, year: int, country: str, retries: int = 3) -> str:
    name = f"{sensor}_{year}_{country}.csv"
    dest = RAW / country / name
    if dest.exists() and dest.stat().st_size > 0:
        return f"cached  {name}"
    ensure_space()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    url = f"{BASE}/{sensor}/{year}/{urllib.parse.quote(name)}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            tmp.rename(dest)
            return f"fetched {name} ({dest.stat().st_size / 1e6:.1f} MB)"
        except urllib.error.HTTPError as e:
            tmp.unlink(missing_ok=True)
            if e.code == 404:  # not published / no fires that year
                return f"skipped {name} (404)"
            err = e
        except Exception as e:  # network hiccup or stall -> wait, then retry
            tmp.unlink(missing_ok=True)
            err = e
        time.sleep(2 ** attempt)
    return f"failed  {name} ({err})"


def latest_archive_year() -> int:
    """Most recent year FIRMS has published as a yearly archive (probed on a country that always burns)."""
    y = dt.date.today().year
    while y > 2012 and not published("modis", y, "Brazil"):
        y -= 1
    return y


def fetch_country(country: str, start: int = 2000, end: int | None = None, progress=None) -> list[str]:
    """Download every sensor-year for `country`. `progress(done, total, msg)` is called per file."""
    end = end or dt.date.today().year
    jobs = [(s, y) for s, first in SENSORS.items() for y in range(max(first, start), end + 1)]
    out = []
    ensure_space()
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(download, s, y, country) for s, y in jobs]
        for i, f in enumerate(cf.as_completed(futs), 1):
            msg = f.result()
            out.append(msg)
            if progress:
                progress(i, len(jobs), msg)
    failed = [m for m in out if m.startswith("failed")]
    if failed:
        raise RuntimeError(f"{len(failed)} downloads failed, e.g. {failed[0]}")
    if not any(m.startswith(("fetched", "cached")) and "modis_" in m for m in out):
        raise RuntimeError(f"FIRMS has no MODIS archive for {country!r}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="Nigeria")
    ap.add_argument("--start", type=int, default=2000)
    ap.add_argument("--end", type=int, default=None, help="default: current year")
    args = ap.parse_args()
    fetch_country(args.country, args.start, args.end, progress=lambda i, n, m: print(m, flush=True))


if __name__ == "__main__":
    main()
