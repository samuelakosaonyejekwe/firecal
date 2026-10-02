"""pipeline/build.py: the build record says which NASA year the country is complete through."""
import json

import pipeline.build as build


def raw_country(root, cid):
    raw = root / "raw" / cid
    raw.mkdir(parents=True)
    (raw / f"modis_2023_{cid}.csv").write_text(
        "latitude,longitude,acq_date,satellite,confidence,frp,type\n7.51,134.61,2023-03-01,Terra,80,12.0,0\n")
    (raw / f"viirs-snpp_2024_{cid}.csv").write_text(
        "latitude,longitude,acq_date,confidence,frp,type\n7.52,134.62,2024-03-01,n,5.0,0\n")


def test_area_without_fires_in_the_latest_year_is_still_complete_through_it(tmp_path, monkeypatch):
    # NASA publishes no MODIS file for a year without fires (here 2024): the record must still say 2024,
    # or every monthly update would re-download the country looking for that year
    monkeypatch.setattr(build, "DATA", tmp_path)
    raw_country(tmp_path, "Palau")
    build.build_country("Palau", keep_raw=True, archive_year=2024)
    rec = json.loads((tmp_path / "countries" / "Palau" / "built.json").read_text())
    assert rec["archive_through"] == 2024
    build.build_country("Palau", keep_raw=True)  # without the published year: the last data year
    assert json.loads((tmp_path / "countries" / "Palau" / "built.json").read_text())["archive_through"] == 2023
