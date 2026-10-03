"""Keep processed countries in step between this computer, GitHub and the website.

Processed countries are stored as assets of the GitHub release `firecal-data`, uploaded with
your own `gh` login (nothing is attributed to bots). `open_store` picks how to reach them: with a
`gh` login that may write to the repository, countries are downloaded and published; without one
(Docker, a fork, a fresh machine) the public release is read over HTTPS and nothing is published. For one country, `sync_country` picks the
cheapest source and makes this computer and GitHub agree:

  only here            -> publish it to GitHub
  only on GitHub       -> download the processed file (up to ~50 MB, not GBs of NASA CSVs)
  both, different      -> the newer copy wins (e.g. a country rebuilt after NASA's next yearly archive)
  neither              -> download from NASA, process, publish

`world.py --update` additionally rebuilds countries whose copy predates NASA's latest yearly
archive (`archive_through` < `latest_archive_year()`). After publishing, ask GitHub to rebuild
the website (`rebuild_site`).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from pipeline.build import build_country
from pipeline.fetch import DATA, fetch_country, latest_archive_year

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = os.environ.get("FIRECAL_REPO", "samuelakosaonyejekwe/firecal")  # this project's repository (a fork sets its own)
RELEASE = "firecal-data"
WORKFLOW = "pages.yml"
FILES = ("grid_daily.parquet", "static_cells.parquet", "built.json")
UNAVAILABLE = "unavailable.json"  # release asset: countries NASA has no usable archive for, with the reason
PERMANENT = ("no VIIRS archive", "has no MODIS archive")  # failures that retrying won't fix


TRANSIENT = ("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "timeout", "connection reset", "EOF")


def gh(*args, waits=(5, 20, 60), timeout=1800) -> str:
    """Run `gh`; GitHub's API sometimes fails for a moment (5xx, timeouts), so those are retried."""
    for wait in (*waits, None):
        try:
            return subprocess.run(["gh", *args], cwd=ROOT, check=True, text=True, capture_output=True, timeout=timeout).stdout
        except subprocess.CalledProcessError as e:
            if wait is None or not any(t.lower() in (e.stderr or "").lower() for t in TRANSIENT):
                raise
        except subprocess.TimeoutExpired:
            if wait is None:
                raise
        time.sleep(wait)


def github_available() -> bool:
    """`gh` installed and logged in, this folder is the repository, and the login may publish to it."""
    if os.environ.get("FIRECAL_NO_GITHUB") == "1" or not shutil.which("gh"):
        return False
    try:  # quick and without retries: the server asks this while starting
        perm = json.loads(gh("repo", "view", "--json", "viewerPermission", waits=(), timeout=60)).get("viewerPermission")
        if perm in ("ADMIN", "MAINTAIN", "WRITE"):
            return True
        # some tokens (e.g. fine-grained ones in GitHub Actions) don't answer that GraphQL field: ask the REST API too
        rest = json.loads(gh("api", "repos/{owner}/{repo}", "--jq", ".permissions", waits=(), timeout=60) or "{}")
        return bool(rest.get("push") or rest.get("maintain") or rest.get("admin"))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return False


def open_store() -> GitHubStore | None:
    """The release to sync with: publishing with this computer's `gh` login when it may write to the repository,
    otherwise read-only over public HTTPS; None when GitHub is switched off (FIRECAL_NO_GITHUB=1, e.g. tests)."""
    if os.environ.get("FIRECAL_NO_GITHUB") == "1":
        return None
    if github_available():
        return GitHubStore()
    try:
        return PublicStore()
    except OSError:  # offline, or GitHub unreachable: work locally
        return None


class GitHubStore:
    """Processed countries as assets of one GitHub release, read and published with `gh`."""
    can_write = True

    def __init__(self):
        self.assets: dict[str, dict] = {}
        self.exists = True
        try:
            data = json.loads(gh("release", "view", RELEASE, "--json", "assets"))
            self.assets = {a["name"]: a for a in data["assets"]}
        except subprocess.CalledProcessError as e:
            if "not found" not in (e.stderr or "").lower():
                raise  # a login or API failure is not "nothing published yet"
            self.exists = False  # created on first publish, never just by looking

    def _asset(self, cid):
        return self.assets.get(f"{cid}.{FILES[0]}")

    def has(self, cid) -> bool:
        return self._asset(cid) is not None

    def unavailable(self) -> dict:
        if UNAVAILABLE not in self.assets:
            return {}
        with tempfile.TemporaryDirectory() as tmp:
            gh("release", "download", RELEASE, "--dir", tmp, "--pattern", UNAVAILABLE, "--clobber")
            return json.loads((pathlib.Path(tmp) / UNAVAILABLE).read_text())

    def mark_unavailable(self, cid, reason):
        """Record once that NASA has no usable archive for a country, so later runs skip it."""
        known = self.unavailable()
        known[cid] = reason
        if not self.exists:
            gh("release", "create", RELEASE, "--title", "FireCal data",
               "--notes", "Harmonized FireCal country grids (0.1° daily fire cell-days). Maintained by pipeline/world.py.")
            self.exists = True
        with tempfile.TemporaryDirectory() as tmp:
            f = pathlib.Path(tmp) / UNAVAILABLE
            f.write_text(json.dumps(known, indent=1, sort_keys=True))
            gh("release", "upload", RELEASE, str(f), "--clobber")
        self.assets[UNAVAILABLE] = {"name": UNAVAILABLE}

    def download(self, cid):
        out = DATA / "countries" / cid
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=out) as tmp:  # same disk -> atomic rename below
            for f in FILES:
                if f"{cid}.{f}" in self.assets:
                    gh("release", "download", RELEASE, "--dir", tmp, "--pattern", f"{cid}.{f}", "--clobber")
            for f in reversed(FILES):  # built.json first: the app never sees a new grid with an old record
                src = pathlib.Path(tmp) / f"{cid}.{f}"
                if src.exists():
                    src.replace(out / f)

    def upload(self, cid):
        if not self.exists:
            gh("release", "create", RELEASE, "--title", "FireCal data",
               "--notes", "Harmonized FireCal country grids (0.1° daily fire cell-days). Maintained by pipeline/world.py.")
            self.exists = True
        with tempfile.TemporaryDirectory() as tmp:
            names = []
            for f in FILES:
                src = DATA / "countries" / cid / f
                if src.exists():
                    dst = pathlib.Path(tmp) / f"{cid}.{f}"
                    shutil.copy2(src, dst)
                    names.append(str(dst))
            gh("release", "upload", RELEASE, *names, "--clobber")
        # refresh our view of what is published
        for a in json.loads(gh("release", "view", RELEASE, "--json", "assets"))["assets"]:
            self.assets[a["name"]] = a

    def local_vs_remote(self, cid) -> str:
        """'same', 'local-newer' or 'remote-newer' for a country present in both places."""
        a = self._asset(cid)
        local = DATA / "countries" / cid / FILES[0]
        if a.get("size") == local.stat().st_size:
            return "same"
        remote_time = dt.datetime.fromisoformat(a["updatedAt"].replace("Z", "+00:00")).timestamp()
        return "local-newer" if local.stat().st_mtime > remote_time else "remote-newer"

    def rebuild_site(self) -> bool:
        try:
            gh("workflow", "run", WORKFLOW)
            return True
        except subprocess.CalledProcessError:
            return False


class PublicStore(GitHubStore):
    """The same release read over public HTTPS: no login needed, and nothing is ever published."""
    can_write = False

    def __init__(self):
        self.exists = True
        api = f"https://api.github.com/repos/{REPO}/releases/tags/{RELEASE}"
        try:
            with urllib.request.urlopen(urllib.request.Request(api, headers={"Accept": "application/vnd.github+json"}),
                                        timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            data, self.exists = {"assets": []}, False
        # the fields gh reports, so the rest of GitHubStore works unchanged
        self.assets = {a["name"]: {"name": a["name"], "size": a["size"], "updatedAt": a["updated_at"],
                                   "url": a["browser_download_url"]} for a in data["assets"]}

    def _get(self, name, dest: pathlib.Path):
        with urllib.request.urlopen(self.assets[name]["url"], timeout=300) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)

    def unavailable(self) -> dict:
        if UNAVAILABLE not in self.assets:
            return {}
        with tempfile.TemporaryDirectory() as tmp:
            f = pathlib.Path(tmp) / UNAVAILABLE
            self._get(UNAVAILABLE, f)
            return json.loads(f.read_text())

    def download(self, cid):
        out = DATA / "countries" / cid
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=out) as tmp:  # same disk -> atomic rename below
            for f in FILES:
                if f"{cid}.{f}" in self.assets:
                    self._get(f"{cid}.{f}", pathlib.Path(tmp) / f"{cid}.{f}")
            for f in reversed(FILES):  # built.json first: the app never sees a new grid with an old record
                src = pathlib.Path(tmp) / f"{cid}.{f}"
                if src.exists():
                    src.replace(out / f)

    def upload(self, cid):
        raise RuntimeError(f"publishing needs a gh login with write access to {REPO}")

    def mark_unavailable(self, cid, reason):
        pass  # only a publisher records it

    def rebuild_site(self) -> bool:
        return False


def archive_through(cid: str) -> int | None:
    """Last NASA yearly archive included in this computer's copy of the country."""
    meta = DATA / "countries" / cid / "built.json"
    if meta.exists():
        return json.loads(meta.read_text()).get("archive_through")
    import duckdb  # older copies without built.json: use the last MODIS date in the grid
    with duckdb.connect() as con:
        y = con.execute(f"SELECT max(year(d)) FROM '{(DATA / 'countries' / cid / FILES[0]).as_posix()}' WHERE s = 0").fetchone()[0]
    return int(y) if y else None


def rebuild(cid: str, store: GitHubStore | None, progress=None) -> tuple[str, bool]:
    """Re-download every year from NASA (picking up newly published archives) and republish."""
    fetch_country(cid, progress=progress)
    build_country(cid, keep_raw=False, archive_year=latest_archive_year())
    if store and store.can_write:
        store.upload(cid)
        return "rebuilt with new NASA years → published", True
    return "rebuilt with new NASA years", False


def drop_raw(cid: str):
    """Delete leftover raw NASA CSVs (and partial downloads) for a country that is now processed."""
    raw = DATA / "raw" / cid
    if raw.is_dir() and (DATA / "countries" / cid / FILES[0]).exists():
        for f in list(raw.glob("*.csv")) + list(raw.glob("*.part")):
            f.unlink()
        if not any(raw.iterdir()):
            raw.rmdir()


def sync_country(cid: str, store: GitHubStore | None, progress=None, skip_published=False) -> tuple[str, bool]:
    """Make this computer (and GitHub, if available) hold the processed country.

    skip_published: leave countries that GitHub already has alone instead of downloading them (used by
    the cloud world build, whose machines start empty). Returns (what happened, whether GitHub changed)."""
    have = (DATA / "countries" / cid / FILES[0]).exists()
    if skip_published and not have and store and store.has(cid):
        return "already published", False
    if have and store and store.can_write:
        if not store.has(cid):
            store.upload(cid)
            return "local → published", True
        state = store.local_vs_remote(cid)
        if state == "local-newer":
            store.upload(cid)
            return "local was newer → published", True
        if state == "remote-newer":
            store.download(cid)
            return "GitHub was newer → downloaded", False
        return "already in sync", False
    if have and store and store.has(cid) and store.local_vs_remote(cid) == "remote-newer":
        store.download(cid)  # read-only: still take a newer published copy
        return "GitHub was newer → downloaded", False
    if have:
        return "local", False
    if store and store.has(cid):
        store.download(cid)
        drop_raw(cid)  # an interrupted NASA download is superseded by the published country
        return "downloaded from GitHub", False
    fetch_country(cid, progress=progress)
    build_country(cid, keep_raw=False, archive_year=latest_archive_year())
    if store and store.can_write:
        store.upload(cid)
        return "built from NASA → published", True
    return "built from NASA", False
