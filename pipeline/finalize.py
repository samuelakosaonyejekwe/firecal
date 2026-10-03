"""Keep the worldwide calibration prior in step with the growing world, without anyone doing anything.

app/resources/prior.json is pooled over the countries it lists. As the preload (local or cloud) adds
countries, this recomputes it once the available countries have grown by at least GROWTH (and by
MIN_NEW), commits only that file under the repository owner's name and pushes it, which rebuilds
the website. It never touches other files, and skips (leaving everything as it is) if this copy of
the repository has diverged from GitHub.

    .venv/bin/python pipeline/finalize.py            # recompute + commit + push when due
    .venv/bin/python pipeline/finalize.py --dry-run  # only report what it would do
    .venv/bin/python pipeline/finalize.py --complete # every country is in: recompute even below the 20% step
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.constants import PRIOR_FILE  # noqa: E402
from pipeline import prior  # noqa: E402

DATA = pathlib.Path(os.environ.get("FIRECAL_DATA", ROOT / "data"))
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang waiting for a password
GROWTH = 0.20  # recompute once 20% more countries are available...
MIN_NEW = 5    # ...and at least 5 more


def git(*args, check=True) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=check, text=True, capture_output=True, env=GIT_ENV,
                          timeout=120).stdout.strip()


def due(available: list[str], complete=False) -> bool:
    """Recompute after GROWTH more countries, and once more when the world is complete (nothing left to build)."""
    current = set(json.loads(PRIOR_FILE.read_text())["countries"])
    new = set(available) - current
    return len(new) >= MIN_NEW and (complete or len(available) >= len(current) * (1 + GROWTH))


def ensure_identity():
    """Commit as the repository owner: use the configured git identity, else the latest commit's author."""
    if not git("config", "user.name", check=False):
        git("config", "user.name", git("log", "-1", "--format=%an"))
        git("config", "user.email", git("log", "-1", "--format=%ae"))


def update_prior(dry_run=False, push=True, complete=False) -> str:
    files = sorted(DATA.glob("countries/*/grid_daily.parquet"))
    available = sorted(f.parent.name for f in files)
    if not files or not due(available, complete):
        return f"calibration prior is up to date ({len(json.loads(PRIOR_FILE.read_text())['countries'])} countries; {len(available)} available)"
    if dry_run:
        return f"would recompute the calibration prior from {len(available)} countries"
    if push:  # start from GitHub's latest version, never over local work
        git("fetch", "-q", "origin", "main")
        if git("rev-list", "--count", "HEAD..origin/main") != "0":
            if subprocess.run(["git", "merge", "-q", "--ff-only", "origin/main"], cwd=ROOT, capture_output=True,
                              env=GIT_ENV, timeout=120).returncode:
                return "skipped: this copy has diverged from GitHub; the calibration prior was left unchanged"
        if not due(available, complete):  # someone else already updated it
            return "calibration prior was already updated on GitHub"
    out = prior.compute(files)
    PRIOR_FILE.write_text(json.dumps(out, indent=1) + "\n")
    msg = f"calibration prior recomputed from {len(available)} countries (k_world={out['k_world']}, k_terra_world={out['k_terra_world']})"
    if not push:
        return msg
    ensure_identity()
    git("add", str(PRIOR_FILE.relative_to(ROOT)))
    git("commit", "-q", "-m", f"Update the worldwide calibration prior from {len(available)} countries",
        "-m", f"Recomputed by pipeline/finalize.py as the world grew: k_world {out['k_world']}, "
              f"k_terra_world {out['k_terra_world']}.", "--", str(PRIOR_FILE.relative_to(ROOT)))
    for _ in range(3):  # if main moved meanwhile, replay this one-file commit on top of it and try again
        if not subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=ROOT, capture_output=True,
                              env=GIT_ENV, timeout=300).returncode:
            return msg + "; committed and pushed (the website rebuilds)"
        git("fetch", "-q", "origin", "main", check=False)
        git("rebase", "-q", "origin/main", check=False)
    if os.environ.get("GITHUB_ACTIONS") == "true":  # a cloud machine is discarded: say what happens instead
        print("::warning::the recomputed calibration prior could not be pushed; the next world build recomputes it")
        return msg + "; committed, but the push failed (the next world build recomputes it)"
    return msg + "; committed, but the push failed (it goes out with your next push)"


if __name__ == "__main__":
    print(update_prior(dry_run="--dry-run" in sys.argv, complete="--complete" in sys.argv))
