"""Fingerprint of this checkout's app (code, page, resources) and its requirements.

The server reports it (/api/health); start.sh compares it with the checkout's and restarts a server that is
running older code. Standard library only.

    python3 app/fingerprint.py
"""
import hashlib
import pathlib

APP = pathlib.Path(__file__).resolve().parent


def fingerprint() -> str:
    h = hashlib.sha1()
    req = APP.parent / "requirements.txt"  # (not in the Docker image, which has its packages built in)
    files = [p for p in APP.rglob("*") if p.is_file() and "__pycache__" not in p.parts] + ([req] if req.exists() else [])
    for p in sorted(files):
        h.update(p.relative_to(APP.parent).as_posix().encode() + b"\0" + p.read_bytes())
    return h.hexdigest()[:12]


if __name__ == "__main__":
    print(fingerprint())
