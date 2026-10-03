"""Preload / sync every FIRMS country, keeping this computer, GitHub and the website in step.

For each country, cheapest source first (see pipeline/sync.py):
  1. on this computer and on GitHub  -> the newer copy wins; nothing to do if they match
  2. only on this computer           -> publish it to GitHub
  3. only on GitHub                  -> download the processed file (up to ~50 MB, not GBs of CSVs)
  4. neither                         -> download from NASA FIRMS, process, publish to GitHub
Raw CSVs are deleted as it goes. Publishing uses your own `gh` login, then asks GitHub to
rebuild the website, so the public app, the repository and your local copy all match.
Resumable: run it again and it carries on where it stopped.

    .venv/bin/python pipeline/world.py               # everything (largest fire regions first)
    .venv/bin/python pipeline/world.py Ghana Chad    # just these
    .venv/bin/python pipeline/world.py --shard 0/8   # one of 8 parallel workers
    .venv/bin/python pipeline/world.py --no-github   # local only
    .venv/bin/python pipeline/world.py --update      # also rebuild countries when NASA publishes a new year
    .venv/bin/python pipeline/world.py --skip-published  # don't download countries GitHub already has (cloud)
    .venv/bin/python pipeline/world.py --missing     # list countries not yet published (nothing is changed)

If NASA can't be reached for several countries in a row (internet down), it stops early with exit
code 3 instead of failing every remaining country; run it again later to continue.
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from pipeline.fetch import latest_archive_year  # noqa: E402
from pipeline.fetch import DATA  # noqa: E402
from pipeline.finalize import update_prior  # noqa: E402
from pipeline.sync import PERMANENT, GitHubStore, archive_through, open_store, rebuild, sync_country  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUBLISH_EVERY = 10         # rebuild the website after this many newly published countries
NETWORK = ("urlopen error", "timed out", "Network is unreachable", "Connection reset", "Temporary failure")
OFFLINE_STOP = 3           # consecutive countries failing on the network before stopping early
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
    if args == ["--missing"]:
        gone = missing()
        print(f"{len(gone)} countries not yet published" + (f": {' '.join(gone)}" if gone else ""))
        return
    unknown = [a for a in args if a.startswith("-") and a not in ("--no-github", "--shard", "--update", "--skip-published")]
    if unknown:
        sys.exit(f"unknown option {unknown[0]}; see --help")
    shard, use_github, update, skip = None, True, "--update" in args, "--skip-published" in args
    for flag in ("--update", "--skip-published"):
        if flag in args:
            args.remove(flag)
    if "--no-github" in args:
        args.remove("--no-github")
        use_github = False
    if "--shard" in args:  # e.g. --shard 3/8 (parallel workers), anywhere on the command line
        at = args.index("--shard")
        i, n = map(int, args[at + 1].split("/"))
        shard, args = (i, n), args[:at] + args[at + 2:]
    ids = args or [c["id"] for c in json.loads((ROOT / "app" / "resources" / "countries.json").read_text(encoding="utf-8"))]
    ids = sorted(ids, key=lambda c: (PRIORITY.index(c) if c in PRIORITY else len(PRIORITY), c))
    if shard:
        ids = shard_of(ids, *shard)

    store = open_store() if use_github else None
    if (shard or skip) and not (store and store.can_write):  # a cloud worker that can't publish would work for nothing
        sys.exit("cannot publish to GitHub (gh missing, not logged in, or without write access to the repository); stopping")
    if use_github and not store:
        print("GitHub not reachable; working locally only", flush=True)
    elif store and not store.can_write:
        print("no gh login with write access: published countries are downloaded, nothing is published", flush=True)
    known = {c["id"] for c in json.loads((ROOT / "app" / "resources" / "countries.json").read_text(encoding="utf-8"))}
    bad = [c for c in ids if c not in known]
    if bad:
        sys.exit(f"unknown country {bad[0]!r}; names look like Nigeria, United_States, Cote_d_Ivoire (see app/resources/countries.json)")

    print(f"{len(ids)} countries to sync" + (f"; {sum(store.has(c) for c in ids)} already on GitHub" if store else ""), flush=True)
    try:
        latest = latest_archive_year() if update else None
    except OSError as e:  # NASA unreachable from this machine even after retries: stop cleanly
        print(f"stopping: NASA unreachable ({e}); run again later (finished countries are kept)", flush=True)
        sys.exit(3)
    if update:
        print(f"NASA's latest yearly archive: {latest}", flush=True)
    failed, published, pending, offline = [], 0, 0, 0
    local_list = DATA / "unavailable.json"  # local copy for the local server
    unavailable = store.unavailable() if store else (json.loads(local_list.read_text()) if local_list.exists() else {})
    if store:
        local_list.parent.mkdir(parents=True, exist_ok=True)
        local_list.write_text(json.dumps(unavailable, indent=1, sort_keys=True))
    for i, cid in enumerate(ids, 1):
        t = time.time()
        if cid in unavailable and not update:  # NASA has no usable archive; recorded on an earlier run
            print(f"[{i}/{len(ids)}] {cid}: skipped, {unavailable[cid]}", flush=True)
            continue
        try:
            how, changed = sync_country(cid, store, skip_published=skip and not update)
            if update and (archive_through(cid) or 0) < latest:
                how, changed2 = rebuild(cid, store)
                changed = changed or changed2
            published += changed
            pending += changed
            offline = 0
            print(f"[{i}/{len(ids)}] {cid}: {how} ({time.time() - t:.0f}s)", flush=True)
        except Exception as e:  # tiny territories may have no VIIRS archive at all
            failed.append(cid)
            print(f"[{i}/{len(ids)}] {cid} FAILED: {e}", flush=True)
            if store and any(p in str(e) for p in PERMANENT):
                store.mark_unavailable(cid, str(e))
                unavailable[cid] = str(e)
                local_list.write_text(json.dumps(unavailable, indent=1, sort_keys=True))
            offline = offline + 1 if any(p in str(e) for p in NETWORK) else 0
            if offline >= OFFLINE_STOP:  # every download fails: this machine can't reach NASA, so stop instead of
                print(f"stopping: NASA unreachable for {offline} countries in a row; "  # failing for hours
                      "run again later to continue (finished countries are kept)", flush=True)
                break
        if store and store.can_write and pending >= PUBLISH_EVERY:
            print("  → asking GitHub to rebuild the website" if store.rebuild_site() else "  (could not trigger the website rebuild)", flush=True)
            pending = 0
    if store and store.can_write and pending:
        print("→ asked GitHub to rebuild the website" if store.rebuild_site() else "(could not trigger the website rebuild)", flush=True)
    print(f"done; {published} published to GitHub, {len(failed)} failed: {failed}")
    if store and not shard:  # a full local run: keep the worldwide calibration in step with the world
        print(update_prior(complete=not failed and len(ids) == len(known)), flush=True)
    if offline >= OFFLINE_STOP:
        sys.exit(3)


def missing() -> list[str]:
    """Countries neither published nor known to be unavailable from NASA (what a retry would still build)."""
    store = GitHubStore()
    gone = store.unavailable()
    ids = [c["id"] for c in json.loads((ROOT / "app" / "resources" / "countries.json").read_text(encoding="utf-8"))]
    return [c for c in ids if c not in gone and not store.has(c)]


if __name__ == "__main__":
    main()
