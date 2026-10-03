"""The website build: every file exists, and the 0.1° box tiles reproduce the server's counts exactly."""
import gzip
import json

import numpy as np
import pytest

import pipeline.static_site as ss
from app.analysis import Store
from app.constants import BOX_TILE, MODIS_START
from tests.conftest import make_empty_country
from tests.test_analysis import RES, make_country


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    make_country(root, "Nigeria", seed=5)
    make_country(root, "Benin", seed=6)
    make_empty_country(root, "Maldives")  # published countries can have no fire records at all
    store = Store(root, RES)
    out, build = tmp_path_factory.mktemp("site"), tmp_path_factory.mktemp("build")
    ss.build_app(out, "test")
    ss.build_meta(store, out, "test")
    ss.build_calendars(store, out)
    ss.build_box_tiles(store, out, build)
    ss.build_own_cells(out, build)
    ss.build_map_layers(store, out, build)
    return store, out, build


def decode(blob):
    raw = gzip.decompress(blob)
    ty, tx = np.frombuffer(raw[4:8], "<i2")
    counts = np.frombuffer(raw[8:20], "<u4")
    off, parts = 20, []
    for n in counts:
        days = np.cumsum(np.frombuffer(raw[off:off + 2 * n], "<u2").astype(np.int64)); off += 2 * n
        idx = np.frombuffer(raw[off:off + 2 * n], "<u2").astype(np.int64); off += 2 * n
        parts.append((days, ty * BOX_TILE + idx // BOX_TILE, tx * BOX_TILE + idx % BOX_TILE))
    return parts  # per sensor: (day, yi, xi)


def test_site_files(built):
    store, out, build = built
    assert '<meta name="firecal-edition" content="static">' in (out / "index.html").read_text()
    assert '"/static/' not in (out / "index.html").read_text()  # relative paths for /firecal/
    assert 'src="static/live.js' in (out / "index.html").read_text() and (out / "static" / "live.js").exists()
    meta = json.loads((out / "data" / "meta.json").read_text())
    assert meta["prior"]["k_world"] == store.prior()[0]
    from app.analysis import AQUA_OUT, MODIS_OUT, VIIRS_GAPS  # the browser engine gets the server's sensor outages
    assert meta["prior"]["viirs_gaps"] == [d.date().isoformat() for d in VIIRS_GAPS]
    assert meta["prior"]["modis_gaps"] == [d.date().isoformat() for d in MODIS_OUT]
    assert meta["prior"]["aqua_gaps"] == [d.date().isoformat() for d in AQUA_OUT]
    assert meta["prior"]["viirs_strips"]["2024-01-31"] and meta["prior"]["strip_deg"] == 10
    assert json.loads((out / "places" / "index.json").read_text())["deg"] == 2  # town names for the map card
    for raw in ("index.html", "sw.js", "manifest.webmanifest"):  # only the processed copies at the site root
        assert not (out / "static" / raw).exists() and (out / raw).exists()
    sw = (out / "sw.js").read_text()
    assert "{{" not in sw and 'const EDITION = "static"' in sw
    for cid in store.ready:
        assert json.loads((out / "data" / "countries" / f"{cid}.json").read_text()) == store.analyze({"country": cid})
    assert (build / "cells_country.parquet").exists()
    assert isinstance(json.loads((out / "data" / "own_cells.json").read_text()), dict)
    for layer in ("all", "y2010", "m01"):
        assert json.loads((out / "data" / "map" / layer / "index.json").read_text())["tiles"]


def test_box_tiles_reproduce_server_counts(built):
    store, out, _ = built
    idx = json.loads((out / "data" / "tiles" / "index.json").read_text())
    assert idx["tile_cells"] == BOX_TILE
    bbox = [2.0, 6.0, 5.0, 9.0]  # spans Nigeria and Benin and several tiles
    y0, y1, x0, x1 = Store._cell_bounds(bbox)
    n_days = (store.end - MODIS_START).days + 1
    counts = np.zeros((3, n_days))
    for key in idx["tiles"]:
        for s, (day, yi, xi) in enumerate(decode((out / "data" / "tiles" / f"{key}.bin.gz").read_bytes())):
            m = (yi >= y0) & (yi <= y1) & (xi >= x0) & (xi <= x1)
            np.add.at(counts[s], day[m], 1)
    files, *_ , b, _ = store.resolve({"bbox": bbox})
    day = store.daily(files, b)
    np.testing.assert_array_equal(counts[0], day.m.to_numpy())
    np.testing.assert_array_equal(counts[2], day.t.to_numpy())
    np.testing.assert_array_equal(np.nan_to_num(counts[1]), np.nan_to_num(day.v.to_numpy()))
