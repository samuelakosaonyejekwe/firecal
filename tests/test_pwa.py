"""Installable app + airplane mode: the service worker saves everything the page loads, and the
manifest's icons and screenshots exist at the sizes it states."""
import json
import pathlib
import re
import struct

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    return struct.unpack(">II", head[16:24])


def test_service_worker_saves_every_script_and_stylesheet_the_page_loads():
    html = (STATIC / "index.html").read_text()
    sw = (STATIC / "sw.js").read_text()
    files = set(json.loads(re.search(r"const STATIC_FILES = (\[[^\]]*\]);", sw).group(1)))
    loaded = set(re.findall(r'"/static/([^"?]+\.(?:js|css))\?v=', html))
    assert loaded and loaded <= files, loaded - files
    for f in files:
        assert (STATIC / f).exists(), f


def test_manifest_icons_and_screenshots_match_their_sizes():
    man = json.loads((STATIC / "manifest.webmanifest").read_text())
    assert man["display"] == "standalone" and man["start_url"] and man["id"]
    assert any(i.get("purpose") == "maskable" for i in man["icons"])
    assert {i["sizes"] for i in man["icons"] if i["type"] == "image/png"} >= {"192x192", "512x512"}
    for item in man["icons"] + man["screenshots"]:
        path = STATIC / item["src"].removeprefix("/static/")
        assert path.exists(), item["src"]
        if item["type"] == "image/png":
            w, h = png_size(path)
            assert f"{w}x{h}" == item["sizes"], (item["src"], w, h)
    html = (STATIC / "index.html").read_text()
    assert 'rel="apple-touch-icon" href="/static/apple-touch-icon.png' in html  # iPhone / iPad home screen
    assert png_size(STATIC / "apple-touch-icon.png") == (180, 180)


def test_scripts_avoid_syntax_older_phones_cannot_run():
    """Older iPhones (iOS 13.4-15) and Android browsers (Chrome 80-91) can't parse or run these;
    one of them anywhere stops the whole app on those phones."""
    newer = {r"\|\|=": "||=", r"\?\?=": "??=", r"&&=": "&&=", r"\.at\(-?\d": ".at()", r"\.replaceAll\(": "replaceAll",
             r"structuredClone\(": "structuredClone", r"Object\.hasOwn\(": "Object.hasOwn", r"\.findLast\(": "findLast",
             r"\d_\d": "numeric separator", r"#\w+\s*[=;(]": "private class field"}
    for f in ("app.js", "data.js", "engine.js", "geo.js", "live.js", "sw.js"):
        src = re.sub(r"`[^`]*`|\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'|//[^\n]*", "", (STATIC / f).read_text())
        found = [name for pat, name in newer.items() if re.search(pat, src)]
        assert not found, (f, found)
