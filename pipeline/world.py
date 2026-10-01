"""Preload / sync every FIRMS country, keeping this computer, GitHub and the website in step.

For each country, cheapest source first (see pipeline/sync.py):
  1. on this computer and on GitHub  -> the newer copy wins; nothing to do if they match
  2. only on this computer           -> publish it to GitHub
  3. only on GitHub                  -> download the processed file (0.01–50 MB, not GBs of CSVs)
  4. neither                         -> download from NASA FIRMS, process, publish to GitHub
Raw CSVs are deleted as it goes. Publishing uses your own `gh` login, then asks GitHub to
rebuild the website, so the public app, the repository and your local copy all match.
Resumable: run it again and it carries on where it stopped.

    .venv/bin/python pipeline/world.py               # everything (largest fire regions first)
    .venv/bin/python pipeline/world.py Ghana Chad    # just these
    .venv/bin/python pipeline/world.py --shard 0/8   # one of 8 parallel workers
    .venv/bin/python pipeline/world.py --no-github   # local only
    .venv/bin/python pipeline/world.py --update      # also rebuild countries when NASA publishes a new year
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from pipeline.fetch import DiskSpaceLow, latest_archive_year  # noqa: E402
from pipeline.sync import GitHubStore, archive_through, github_available, rebuild, sync_country  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUBLISH_EVERY = 10         # rebuild the website after this many newly published countries
PRIORITY = ["Democratic_Republic_of_the_Congo", "Brazil", "Russian_Federation", "Angola", "Australia", "Zambia",
            "Mozambique", "Central_African_Republic", "South_Sudan", "United_States", "Argentina", "India",
            "Indonesia", "Canada", "Bolivia", "Tanzania", "Sudan", "Nigeria", "Chad", "Kazakhstan"]


def shard_of(ids, i, n):
    """Spread countries over n workers, balancing the big fire regions across them."""
    return [c for k, c in enumerate(ids) if k % n == i]


def main():
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return
    unknown = [a for a in args if a.startswith("-") and a not in ("--no-github", "--shard", "--update")]
    if unknown:
        sys.exit(f"unknown option {unknown[0]}; see --help")
    shard, use_github, update = None, True, "--update" in args
    if update:
        args.remove("--update")
    if "--no-github" in args:
        args.remove("--no-github")
        use_github = False
    if args and args[0] == "--shard":  # e.g. --shard 3/8 (parallel workers)
        i, n = map(int, args[1].split("/"))
        shard, args = (i, n), args[2:]
    ids = args or [c["id"] for c in json.loads((ROOT / "app" / "resources" / "countries.json").read_text(encoding="utf-8"))]
    ids = sorted(ids, key=lambda c: (PRIORITY.index(c) if c in PRIORITY else len(PRIORITY), c))
    if shard:
        ids = shard_of(ids, *shard)

    store = GitHubStore() if use_github and github_available() else None
    if use_github and not store:
        print("GitHub not available (gh missing, not logged in, or not the repo folder); working locally only", flush=True)
    known = {c["id"] for c in json.loads((ROOT / "app" / "resources" / "countries.json").read_text(encoding="utf-8"))}
    bad = [c for c in ids if c not in known]
    if bad:
        sys.exit(f"unknown country {bad[0]!r}; names look like Nigeria, United_States, Cote_d_Ivoire (see app/resources/countries.json)")

    print(f"{len(ids)} countries to sync" + (f"; {sum(store.has(c) for c in ids)} already on GitHub" if store else ""), flush=True)
    latest = latest_archive_year() if update else None
    if update:
        print(f"NASA's latest yearly archive: {latest}", flush=True)
    failed, published, pending = [], 0, 0
    for i, cid in enumerate(ids, 1):
        t = time.time()
        try:
            how, changed = sync_country(cid, store)
            if update and (archive_through(cid) or 0) < latest:
                how, changed2 = rebuild(cid, store)
                changed = changed or changed2
            published += changed
            pending += changed
            print(f"[{i}/{len(ids)}] {cid}: {how} ({time.time() - t:.0f}s)", flush=True)
        except DiskSpaceLow as e:  # stop cleanly; nothing is lost and the next run resumes here
            print(f"[{i}/{len(ids)}] {cid}: {e}", flush=True)
            break
        except Exception as e:  # tiny territories may have no VIIRS archive at all
            failed.append(cid)
            print(f"[{i}/{len(ids)}] {cid} FAILED: {e}", flush=True)
        if store and pending >= PUBLISH_EVERY:
            print("  → asking GitHub to rebuild the website" if store.rebuild_site() else "  (could not trigger the website rebuild)", flush=True)
            pending = 0
    if store and pending:
        print("→ asked GitHub to rebuild the website" if store.rebuild_site() else "(could not trigger the website rebuild)", flush=True)
    print(f"done; {published} published to GitHub, {len(failed)} failed: {failed}")


if __name__ == "__main__":
    main()
