"""One-off: build app/resources/places/ (place names for the map's hover card) from GeoNames.

Every town and city on Earth with at least 1 000 people (GeoNames cities1000, CC BY 4.0), with its
first-level region (state, province, region…), in 2° × 2° tiles. Each tile holds every town that can be
the answer for some point inside it (its own towns, plus nearer neighbours' towns where it is sparse), so
naming any square takes one small download. The answer is the town with the smallest distance / pull,
pull = 1 + 0.25·log10(population / 1000), so a city 15 km away is preferred to a hamlet 9 km away; the
browser (app/static/data.js, place()) applies the same rule. Tiles with no town within MAX_KM of any of their
points are left out (open ocean, ice sheets); in every other tile the answer is exactly the global one:

  * index.json    : {"deg": 2, "tiles": ["ty_tx", …], "countries": {"NG": "Nigeria", …}} (FireCal's spelling)
  * <ty>_<tx>.json: {"a": [region names], "c": [country codes], "p": [[name, lat×1000, lon×1000, a, c, pop], …]}
                    tile ty_tx covers latitudes −90 + 2·ty … +2, longitudes −180 + 2·tx … +2

The browser (app/static/data.js, place()) names the square from the nearest notable place, so the
card reads e.g. "18 km NE of Ogbomosho · Oyo".

    .venv/bin/python pipeline/places.py
"""
import io
import json
import math
import pathlib
import re
import shutil
import unicodedata
import urllib.request
import zipfile

import numpy as np

DUMP = "https://download.geonames.org/export/dump/"
RES = pathlib.Path(__file__).resolve().parent.parent / "app" / "resources"
OUT = RES / "places"
DEG = 2
KM, MAX_KM, MAX_PULL, GRID = 111.2, 250, 2.1, 33  # km per degree, farthest answer, largest pull, samples per tile side
PER_SQUARE = 2  # FireCal's squares are 0.1°: their two biggest towns are enough to name them
# GeoNames country names that FireCal spells differently (countries.json): the card compares them
FIRECAL_NAME = {"CV": "Cape Verde", "CI": "Côte d'Ivoire", "CZ": "Czech Republic", "NL": "Netherlands",
                "PS": "Palestine", "GM": "The Gambia"}


def wrap(d):
    """Longitude difference in degrees, the short way round (across the 180° line where that is shorter)."""
    return (d + 180) % 360 - 180


def norm(s):
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())


def fetch(name):
    with urllib.request.urlopen(DUMP + name, timeout=300) as r:
        return r.read()


def lines(text):
    return (ln.split("\t") for ln in text.splitlines() if ln and not ln.startswith("#"))


def main():
    firecal = {norm(c["name"]): c["name"] for c in json.loads((RES / "countries.json").read_text(encoding="utf-8"))}
    countries = {f[0]: FIRECAL_NAME.get(f[0]) or firecal.get(norm(f[4]), f[4])
                 for f in lines(fetch("countryInfo.txt").decode("utf-8")) if len(f) > 4}
    regions = {f[0]: f[1] for f in lines(fetch("admin1CodesASCII.txt").decode("utf-8")) if len(f) > 1}
    with zipfile.ZipFile(io.BytesIO(fetch("cities1000.zip"))) as z:
        cities = z.read("cities1000.txt").decode("utf-8")

    rows = []
    for f in lines(cities):
        rows.append((f[1], float(f[4]), float(f[5]), regions.get(f"{f[8]}.{f[10]}", ""), f[8], int(f[14] or 0)))
    squares = {}
    for r in sorted(rows, key=lambda r: -r[5]):  # biggest first; dense areas keep their main towns, sparse ones all
        squares.setdefault((math.floor(r[1] * 10), math.floor(r[2] * 10)), []).append(r)
    rows = [r for sq in squares.values() for r in sq[:PER_SQUARE]]
    lat, lon = np.array([r[1] for r in rows]), np.array([r[2] for r in rows])
    pull = 1 + 0.25 * np.log10(np.maximum([r[5] for r in rows], 1000) / 1000)
    ny, nx = 180 // DEG, 360 // DEG
    # a tile is kept when some point in it has a town within MAX_KM; its farthest point is then at most MAX_KM plus the
    # tile's diagonal from that town, so towns within that distance (× the largest pull) are all the candidates needed
    reach = (MAX_KM + math.hypot(DEG, DEG) * KM) * MAX_PULL / KM  # degrees of latitude
    order = np.argsort(lat)
    tiles = {}
    for ty in range(ny):
        s_, n_ = -90 + ty * DEG, -90 + (ty + 1) * DEG
        if ty % 10 == 0:
            print(f"  latitude {s_}°…", flush=True)
        lo_i, hi_i = np.searchsorted(lat[order], [s_ - reach, n_ + reach])
        band = order[lo_i:hi_i]  # towns within reach of this row of tiles (by latitude)
        if not len(band):
            continue
        cos_min = math.cos(math.radians(min(89.9, max(abs(s_), abs(n_)))))  # narrowest longitude degree in the row
        lon_reach = min(180.0, reach / max(cos_min, 1e-3) + DEG)
        for tx in range(nx):
            w_, e_ = -180 + tx * DEG, -180 + (tx + 1) * DEG
            dlon_c = np.abs((lon[band] - (w_ + e_) / 2 + 180) % 360 - 180)
            cand = band[dlon_c <= lon_reach]
            if not len(cand):
                continue
            def scores(grid, cand):
                gy, gx = np.meshgrid(np.linspace(s_, n_, grid), np.linspace(w_, e_, grid))
                py, px = gy.ravel(), gx.ravel()
                dy = (py[:, None] - lat[cand]) * KM
                dx = wrap(px[:, None] - lon[cand]) * KM * np.cos(np.radians(py))[:, None]
                return np.hypot(dx, dy) / pull[cand]
            # coarse pass: the worst best answer in the tile bounds which towns can matter at all
            coarse = scores(5, cand).min(axis=1)
            if coarse.min() > MAX_KM:  # no town within reach anywhere in the tile (open ocean, ice)
                continue
            upper = coarse.max() + 0.5 * math.hypot(DEG / 4 * KM, DEG / 4 * KM * cos_min)
            dlat = np.maximum(0, np.maximum(s_ - lat[cand], lat[cand] - n_))
            mid = (w_ + e_) / 2  # distance in longitude to the tile, across the 180° line where shorter
            dlon = np.maximum(0, np.abs(wrap(lon[cand] - mid)) - DEG / 2)
            cand = cand[np.hypot(dlat * KM, dlon * KM * cos_min) / pull[cand] <= upper]
            score = scores(GRID, cand)
            best = score.min(axis=1)
            # Every point lies within `half` km of a sample; moving that far changes any score by at most `half`, so a
            # town can win somewhere in the tile only if, at some sample, it is within 2·half of the best there.
            half = 0.5 * math.hypot(DEG / (GRID - 1) * KM, DEG / (GRID - 1) * KM * cos_min)
            keep = cand[(score - best[:, None]).min(axis=0) <= 2 * half]
            tiles[(ty, tx)] = [rows[i] for i in keep]

    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    used = set()
    for (ty, tx), trows in sorted(tiles.items()):
        trows.sort(key=lambda r: -r[5])  # biggest first
        a, c = sorted({r[3] for r in trows}), sorted({r[4] for r in trows})
        ai, ci = {v: i for i, v in enumerate(a)}, {v: i for i, v in enumerate(c)}
        used.update(c)
        p = [[r[0], round(r[1] * 1000), round(r[2] * 1000), ai[r[3]], ci[r[4]], r[5]] for r in trows]
        (OUT / f"{ty}_{tx}.json").write_text(json.dumps({"a": a, "c": c, "p": p}, ensure_ascii=False, separators=(",", ":")),
                                            encoding="utf-8")
    index = {"deg": DEG, "tiles": [f"{ty}_{tx}" for ty, tx in sorted(tiles)],
             "countries": {k: v for k, v in sorted(countries.items()) if k in used}}
    (OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size = sum(f.stat().st_size for f in OUT.iterdir())
    print(f"{len(rows)} places kept, {len(tiles)} tiles, {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
