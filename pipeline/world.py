"""Preload every FIRMS country (≈30 GB download, ≈1 GB gridded; raw CSVs deleted as it goes).

Resumable: countries already processed are skipped. Run it before a demo so every
country opens instantly:

    .venv/bin/python pipeline/world.py            # all 208 countries, largest fire regions first
    .venv/bin/python pipeline/world.py Ghana Chad # just these
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from pipeline.build import build_country  # noqa: E402
from pipeline.fetch import DATA, fetch_country  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
PRIORITY = ["Democratic_Republic_of_the_Congo", "Brazil", "Russian_Federation", "Angola", "Australia", "Zambia",
            "Mozambique", "Central_African_Republic", "South_Sudan", "United_States", "Argentina", "India",
            "Indonesia", "Canada", "Bolivia", "Tanzania", "Sudan", "Nigeria", "Chad", "Kazakhstan"]


def main():
    ids = sys.argv[1:] or [c["id"] for c in json.load(open(ROOT / "app" / "resources" / "countries.json"))]
    ids = sorted(ids, key=lambda c: (PRIORITY.index(c) if c in PRIORITY else len(PRIORITY), c))
    done = {p.parent.name for p in (DATA / "countries").glob("*/grid_daily.parquet")}
    todo = [c for c in ids if c not in done]
    print(f"{len(done)} already processed, {len(todo)} to go", flush=True)
    failed = []
    for i, cid in enumerate(todo, 1):
        t = time.time()
        try:
            fetch_country(cid)
            build_country(cid, keep_raw=False)
            print(f"[{i}/{len(todo)}] {cid} ok ({time.time() - t:.0f}s)", flush=True)
        except Exception as e:  # tiny territories may have no VIIRS archive at all
            failed.append(cid)
            print(f"[{i}/{len(todo)}] {cid} FAILED: {e}", flush=True)
    print(f"done; {len(failed)} failed: {failed}")


if __name__ == "__main__":
    main()
