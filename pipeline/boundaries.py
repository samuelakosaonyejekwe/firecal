"""One-off: build app/resources/{countries.json, world.geojson} from Natural Earth.

Maps each FIRMS country-file name to a Natural Earth admin-0 shape:
  * countries.json : [{id, name, bbox}] for all FIRMS countries (search + AOI intersection)
  * world.geojson  : simplified 110m outlines keyed by FIRMS id (clickable world map)
  * shapes.geojson : 50m outlines keyed by FIRMS id (server-side AOI -> country lookup)
Territories FIRMS lists separately but Natural Earth folds into a parent (e.g. Guadeloupe)
get no shape; they remain searchable and their bbox comes from the data once processed.
"""
import json
import pathlib
import re
import sys
import unicodedata
import urllib.request

from shapely.geometry import mapping, shape

NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_{}m_admin_0_countries.geojson"
RES = pathlib.Path(__file__).resolve().parent.parent / "app" / "resources"
ALIASES = {
    "Cote_d_Ivoire": "Ivory Coast", "Swaziland": "eSwatini", "Lao_PDR": "Laos", "Republic_of_Korea": "South Korea",
    "Republic_of_Congo": "Republic of the Congo", "Timor-Leste": "East Timor", "Cape_Verde": "Cabo Verde",
    "The_Gambia": "Gambia", "Russian_Federation": "Russia", "Brunei_Darussalam": "Brunei", "Czech_Republic": "Czechia",
    "Hong_Kong": "Hong Kong S.A.R.", "Serbia": "Republic of Serbia", "Tanzania": "United Republic of Tanzania",
    "Bahamas": "The Bahamas", "United_States": "United States of America",
}
NO_SHAPE = {"Guadeloupe", "Martinique", "Mayotte", "French_Guiana", "United_States_Minor_Outlying_Islands"}
DISPLAY = {"Cote_d_Ivoire": "Côte d'Ivoire", "Lao_PDR": "Laos", "Republic_of_Korea": "South Korea",
           "Republic_of_Congo": "Republic of the Congo", "Swaziland": "Eswatini", "Russian_Federation": "Russia",
           "Brunei_Darussalam": "Brunei"}


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", s)


def load(res):
    return json.load(urllib.request.urlopen(NE.format(res), timeout=120))["features"]


def index(features):
    idx = {}
    for key in ("ADMIN", "NAME_LONG", "NAME", "FORMAL_EN", "BRK_NAME", "NAME_EN"):  # priority order
        for f in features:
            v = f["properties"].get(key)
            if v:
                idx.setdefault(norm(v), f)
    return idx


def main():
    firms = json.load(open(RES / "firms_countries.json"))
    ne50, ne110 = load(50), load(110)
    i50, i110 = index(ne50), index(ne110)
    meta, shapes, world = [], [], []
    for fid in firms:
        name = DISPLAY.get(fid, fid.replace("_", " "))
        f50 = None if fid in NO_SHAPE else i50.get(norm(ALIASES.get(fid, fid.replace("_", " "))))
        bbox = view = None
        if f50:
            g = shape(f50["geometry"])
            bbox = [round(v, 3) for v in g.bounds]
            main = max(getattr(g, "geoms", [g]), key=lambda p: p.area)  # zoom target: main landmass
            view = [round(v, 3) for v in main.buffer(max(0.3, (main.area ** 0.5) * 0.08)).bounds]
            shapes.append({"type": "Feature", "properties": {"id": fid}, "geometry": mapping(g)})
            f110 = i110.get(norm(f50["properties"]["ADMIN"]))
            gw = shape(f110["geometry"]) if f110 else g.simplify(0.05, preserve_topology=True)
            world.append({"type": "Feature", "properties": {"id": fid, "name": name},
                          "geometry": mapping(gw.simplify(0.02, preserve_topology=True))})
        meta.append({"id": fid, "name": name, "bbox": bbox, "view": view})
    json.dump(meta, open(RES / "countries.json", "w"), ensure_ascii=False, separators=(",", ":"))
    json.dump({"type": "FeatureCollection", "features": shapes}, open(RES / "shapes.geojson", "w"), separators=(",", ":"))
    json.dump({"type": "FeatureCollection", "features": world}, open(RES / "world.geojson", "w"),
              ensure_ascii=False, separators=(",", ":"))
    print(f"{len(meta)} countries, {len(shapes)} with shapes", file=sys.stderr)


if __name__ == "__main__":
    main()
