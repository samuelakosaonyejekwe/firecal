"""Preload / sync every FIRMS country, keeping this computer, GitHub and the website in step.

For each country, cheapest source first:
  1. already processed on this computer          -> keep it (and publish it to GitHub if it isn't there yet)
  2. already published to GitHub (release data)  -> download the small processed file (~5 MB, not GBs of CSVs)
  3. otherwise                                    -> download from NASA FIRMS, process, publish to GitHub
Raw CSVs are deleted as it goes. Publishing uses your own `gh` login, then asks GitHub to
rebuild the website, so the public app, the repository and your local copy all match.
Resumable: run it again and it carries on where it stopped.

    .venv/bin/python pipeline/world.py               # everything (largest fire regions first)
    .venv/bin/python pipeline/world.py Ghana Chad    # just these
    .venv/bin/python pipeline/world.py --shard 0/8   # one of 8 parallel workers
    .venv/bin/python pipeline/world.py --no-github   # local only
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from pipeline.build import build_country  # noqa: E402
from pipeline.fetch import DATA, fetch_country  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
RELEASE = "firecal-data"   # GitHub release that stores processed countries
WORKFLOW = "pages.yml"     # website build
PUBLISH_EVERY = 10         # rebuild the website after this many newly published countries
PRIORITY = ["Democratic_Republic_of_the_Congo", "Brazil", "Russian_Federation", "Angola", "Australia", "Zambia",
            "Mozambique", "Central_African_Republic", "South_Sudan", "United_States", "Argentina", "India",
            "Indonesia", "Canada", "Bolivia", "Tanzania", "Sudan", "Nigeria", "Chad", "Kazakhstan"]
FILES = {"grid": "grid_daily.parquet", "static": "static_cells.parquet"}


def shard_of(ids, i, n):
    """Spread countries over n workers, balancing the big fire regions across them."""
    return [c for k, c in enumerate(ids) if k % n == i]


def gh(*args, capture=True):
    return subprocess.run(["gh", *args], cwd=ROOT, check=True, text=True, capture_output=capture).stdout


class GitHubStore:
    """Processed countries as assets of one GitHub release (uploaded with the user's own login)."""

    def __init__(self):
        try:
            gh("release", "view", RELEASE, "--json", "tagName")
        except subprocess.CalledProcessError:
            gh("release", "create", RELEASE, "--title", "FireCal data",
               "--notes", "Harmonized FireCal country grids (0.1° daily fire cell-days). Updated by pipeline/world.py.")
        self.assets = {a["name"] for a in json.loads(gh("release", "view", RELEASE, "--json", "assets"))["assets"]}

    def has(self, cid):
        return f"{cid}.{FILES['grid']}" in self.assets

    def download(self, cid):
        out = DATA / "countries" / cid
        with tempfile.TemporaryDirectory() as tmp:
            gh("release", "download", RELEASE, "--dir", tmp, "--pattern", f"{cid}.*")
            out.mkdir(parents=True, exist_ok=True)
            for f in FILES.values():
                src = pathlib.Path(tmp) / f"{cid}.{f}"
                if src.exists():
                    shutil.move(src, out / f)

    def upload(self, cid):
        with tempfile.TemporaryDirectory() as tmp:
            names = []
            for f in FILES.values():
                src = DATA / "countries" / cid / f
                if src.exists():
                    dst = pathlib.Path(tmp) / f"{cid}.{f}"
                    shutil.copy(src, dst)
                    names.append(str(dst))
            gh("release", "upload", RELEASE, *names, "--clobber")
        self.assets.update(f"{cid}.{f}" for f in FILES.values())

    def rebuild_site(self):
        try:
            gh("workflow", "run", WORKFLOW)
            print("  → asked GitHub to rebuild the website", flush=True)
        except subprocess.CalledProcessError as e:
            print(f"  (could not trigger the website rebuild: {e.stderr.strip() if e.stderr else e})", flush=True)


def main():
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return
    unknown = [a for a in args if a.startswith("-") and a not in ("--no-github", "--shard")]
    if unknown:
        sys.exit(f"unknown option {unknown[0]}; see --help")
    shard, use_github = None, True
    if "--no-github" in args:
        args.remove("--no-github")
        use_github = False
    if args and args[0] == "--shard":  # e.g. --shard 3/8 (parallel workers)
        i, n = map(int, args[1].split("/"))
        shard, args = (i, n), args[2:]
    ids = args or [c["id"] for c in json.load(open(ROOT / "app" / "resources" / "countries.json"))]
    ids = sorted(ids, key=lambda c: (PRIORITY.index(c) if c in PRIORITY else len(PRIORITY), c))
    if shard:
        ids = shard_of(ids, *shard)

    store = None
    if use_github and shutil.which("gh"):
        try:
            store = GitHubStore()
        except subprocess.CalledProcessError as e:
            print(f"GitHub not available ({e.stderr.strip() if e.stderr else e}); continuing locally only", flush=True)

    local = {p.parent.name for p in (DATA / "countries").glob("*/grid_daily.parquet")}
    print(f"{len(local)} countries on this computer"
          + (f", {sum(store.has(c) for c in ids)} on GitHub" if store else "") + f", {len(ids)} requested", flush=True)
    failed, published = [], 0
    for i, cid in enumerate(ids, 1):
        t, how = time.time(), ""
        try:
            if cid in local:
                how = "local"
                if store and not store.has(cid):
                    store.upload(cid)
                    published += 1
                    how = "local → published"
            elif store and store.has(cid):
                store.download(cid)
                how = "downloaded from GitHub"
            else:
                fetch_country(cid)
                build_country(cid, keep_raw=False)
                how = "built from NASA"
                if store:
                    store.upload(cid)
                    published += 1
                    how += " → published"
            print(f"[{i}/{len(ids)}] {cid} ok, {how} ({time.time() - t:.0f}s)", flush=True)
        except Exception as e:  # tiny territories may have no VIIRS archive at all
            failed.append(cid)
            print(f"[{i}/{len(ids)}] {cid} FAILED: {e}", flush=True)
        if store and published and published % PUBLISH_EVERY == 0 and how.endswith("published"):
            store.rebuild_site()
    if store and published:
        store.rebuild_site()
    print(f"done; {published} newly published, {len(failed)} failed: {failed}")


if __name__ == "__main__":
    main()
