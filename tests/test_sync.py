"""pipeline/sync.py against a simulated GitHub release (no network, no account)."""
import json
import os
import pathlib
import shutil
import time

import pytest

import pipeline.sync as sync


class FakeGitHub:
    """Minimal stand-in for the `gh` commands sync.py uses, backed by a folder."""

    def __init__(self, root: pathlib.Path):
        self.dir = root / "release"
        self.exists = False
        self.workflow_runs = 0

    def __call__(self, *args):
        cmd = args[:2]
        if cmd == ("release", "view"):
            if not self.exists:
                raise sync.subprocess.CalledProcessError(1, "gh", stderr="release not found")
            assets = [{"name": p.name, "size": p.stat().st_size,
                       "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.stat().st_mtime))}
                      for p in sorted(self.dir.glob("*"))]
            return json.dumps({"assets": assets})
        if cmd == ("release", "create"):
            self.exists = True
            self.dir.mkdir(parents=True, exist_ok=True)
            return ""
        if cmd == ("release", "upload"):
            for f in args[3:]:
                if not f.startswith("--"):
                    shutil.copy(f, self.dir / pathlib.Path(f).name)
            return ""
        if cmd == ("release", "download"):
            dest, pattern = pathlib.Path(args[args.index("--dir") + 1]), args[args.index("--pattern") + 1]
            shutil.copy(self.dir / pattern, dest / pattern)
            return ""
        if cmd == ("workflow", "run"):
            self.workflow_runs += 1
            return ""
        raise AssertionError(f"unexpected gh call {args}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakeGitHub(tmp_path)
    monkeypatch.setattr(sync, "gh", fake)
    monkeypatch.setattr(sync, "DATA", tmp_path / "data")
    built = []

    def fake_fetch(cid, progress=None):
        built.append(cid)

    def fake_build(cid, keep_raw=False):
        out = tmp_path / "data" / "countries" / cid
        out.mkdir(parents=True, exist_ok=True)
        (out / "grid_daily.parquet").write_bytes(b"built-" + cid.encode())

    monkeypatch.setattr(sync, "fetch_country", fake_fetch)
    monkeypatch.setattr(sync, "build_country", fake_build)
    return fake, tmp_path / "data" / "countries", built


def local(root, cid, content):
    d = root / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "grid_daily.parquet").write_bytes(content)
    return d / "grid_daily.parquet"


def test_looking_never_creates_the_release(env):
    fake, _, _ = env
    store = sync.GitHubStore()
    assert not store.exists and not fake.exists and not store.has("Chad")


def test_local_only_is_published(env):
    fake, root, _ = env
    local(root, "Chad", b"chad-v1")
    how, changed = sync.sync_country("Chad", sync.GitHubStore())
    assert changed and "published" in how and (fake.dir / "Chad.grid_daily.parquet").read_bytes() == b"chad-v1"


def test_remote_only_is_downloaded_not_rebuilt(env):
    fake, root, built = env
    fake("release", "create")
    (fake.dir / "Mali.grid_daily.parquet").write_bytes(b"mali")
    how, changed = sync.sync_country("Mali", sync.GitHubStore())
    assert not changed and "downloaded" in how and not built
    assert (root / "Mali" / "grid_daily.parquet").read_bytes() == b"mali"


def test_newer_copy_wins_both_ways(env):
    fake, root, _ = env
    fake("release", "create")
    (fake.dir / "Togo.grid_daily.parquet").write_bytes(b"old")
    os.utime(fake.dir / "Togo.grid_daily.parquet", (1e9, 1e9))
    local(root, "Togo", b"rebuilt-newer")
    how, changed = sync.sync_country("Togo", sync.GitHubStore())
    assert changed and "local was newer" in how and (fake.dir / "Togo.grid_daily.parquet").read_bytes() == b"rebuilt-newer"

    (fake.dir / "Togo.grid_daily.parquet").write_bytes(b"newer-from-another-machine")
    os.utime(root / "Togo" / "grid_daily.parquet", (1e9, 1e9))
    how, changed = sync.sync_country("Togo", sync.GitHubStore())
    assert not changed and "GitHub was newer" in how
    assert (root / "Togo" / "grid_daily.parquet").read_bytes() == b"newer-from-another-machine"
    assert sync.sync_country("Togo", sync.GitHubStore()) == ("already in sync", False)


def test_neither_builds_from_nasa_and_publishes(env):
    fake, root, built = env
    how, changed = sync.sync_country("Niger", sync.GitHubStore())
    assert built == ["Niger"] and changed and fake.exists
    assert (fake.dir / "Niger.grid_daily.parquet").read_bytes() == b"built-Niger"


def test_without_github_stays_local(env):
    _, root, built = env
    assert sync.sync_country("Benin", None) == ("built from NASA", False) and built == ["Benin"]


def test_cloud_skips_published_countries_without_downloading(env):
    fake, root, built = env
    fake("release", "create")
    (fake.dir / "Mali.grid_daily.parquet").write_bytes(b"mali")
    assert sync.sync_country("Mali", sync.GitHubStore(), skip_published=True) == ("already published", False)
    assert not (root / "Mali").exists() and not built
