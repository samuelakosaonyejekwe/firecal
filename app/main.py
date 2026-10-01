"""FireCal API + web app.

Run:  .venv/bin/uvicorn app.main:app --port 8765   ->  http://127.0.0.1:8765
"""
import hashlib
import logging
import os
import pathlib
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .analysis import Store, NeedsData
from .constants import MODIS_START, SERVER_MAX_BOX_DEG2 as MAX_BOX_DEG2, VIIRS_START
from .jobs import Jobs
from .nrt import NRTFeed

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = pathlib.Path(os.environ.get("FIRECAL_DATA", ROOT / "data"))
STATIC = pathlib.Path(__file__).resolve().parent / "static"
RES = pathlib.Path(__file__).resolve().parent / "resources"

log = logging.getLogger("firecal")
store = Store(DATA, RES)
jobs = Jobs(on_ready=lambda cid: store.refresh())
nrt = NRTFeed(DATA)


@asynccontextmanager
async def lifespan(_app):
    # background work starts with the server, not on import (tests and tools import this module)
    if os.environ.get("FIRECAL_NO_LIVE") != "1":
        nrt.start()
        # backs up GitHub's best-effort schedule: rebuild the website when it falls behind NASA
        from pipeline import keeper
        keeper.start(log=log.warning)
    yield


app = FastAPI(title="FireCal — harmonized MODIS/VIIRS burning calendar", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)

# cache-busting version for static assets: changes whenever a file changes
VERSION = hashlib.sha1(b"".join(p.read_bytes() for p in sorted(STATIC.rglob("*")) if p.is_file())).hexdigest()[:10]


def cached(data, seconds: int):
    return JSONResponse(data, headers={"Cache-Control": f"public, max-age={seconds}"})


def parse_bbox(bbox: str):
    try:
        w, s, e, n = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(400, "bbox must be west,south,east,north")
    w, e = max(-180.0, w), min(180.0, e)
    s, n = max(-90.0, s), min(90.0, n)
    if not (w < e and s < n):
        raise HTTPException(400, "bbox is empty (west must be < east and south < north)")
    return [round(w, 3), round(s, 3), round(e, 3), round(n, 3)]


def parse_aoi(country: str | None, bbox: str | None) -> dict:
    store.refresh()  # notice countries added or removed on disk since the last request (a cheap glob)
    if country:
        if country not in store.meta:
            raise HTTPException(404, f"unknown country {country!r}")
        return {"country": country}
    if bbox:
        b = parse_bbox(bbox)
        if (b[2] - b[0]) * (b[3] - b[1]) > MAX_BOX_DEG2:
            raise HTTPException(400, "area too large: pick a country or draw a smaller box")
        return {"bbox": b}
    raise HTTPException(400, "pass country=<id> or bbox=w,s,e,n")


def needs_data(e: NeedsData):
    # 202 Accepted: a normal answer ("not ready yet"), not a client error
    return JSONResponse(status_code=202, content={
        "needs_data": True, "detail": "data not processed yet", "missing": [{"id": c, "name": store.meta[c]["name"]} for c in e.missing],
        "jobs": {c: jobs.status(c) for c in e.missing}})


# ─────────────────────────────── API ───────────────────────────────
@app.get("/api/health")
def health():
    return {"ok": True, "countries_ready": len(store.ready), "live_feed": nrt.fetched_at(), "live_error": nrt.error}


@app.get("/api/meta")
def meta():
    store.refresh()
    ready = set(store.ready)
    return JSONResponse({
        "countries": [{"id": c["id"], "name": c["name"], "view": store.view(c["id"]), "ready": c["id"] in ready}
                      for c in sorted(store.meta.values(), key=lambda c: c["name"])],
        "range": {"start": MODIS_START.date().isoformat(), "end": store.end.date().isoformat(),
                  "viirs_start": VIIRS_START.date().isoformat()},
        "prior": dict(zip(("k_world", "k_terra_world"), store.prior())), "max_box_deg2": MAX_BOX_DEG2,
        "jobs": jobs.status(), "version": VERSION,
    }, headers={"Cache-Control": "no-cache"})


@app.get("/api/calendar")
def calendar(country: str | None = None, bbox: str | None = None):
    aoi = parse_aoi(country, bbox)
    try:
        return Response(store.analyze_bytes(aoi), media_type="application/json",
                        headers={"Cache-Control": "public, max-age=3600"})
    except NeedsData as e:
        return needs_data(e)


@app.get("/api/grid")
def grid(bbox: str, year: int | None = Query(None, ge=2000, le=2100), month: int | None = Query(None, ge=1, le=12)):
    return cached(store.grid(parse_bbox(bbox), year, month), 3600)


@app.get("/api/live")
def live(bbox: str = "-180,-90,180,90"):
    df = nrt.frame()
    if df is None:
        return JSONResponse({"available": False, "reason": nrt.error or "live feed is loading, try again shortly"},
                            status_code=503)
    return cached({**store.live(df, parse_bbox(bbox)), "fetched_at": nrt.fetched_at()}, 600)


@app.get("/api/nowcast")
def nowcast(country: str | None = None, bbox: str | None = None):
    aoi = parse_aoi(country, bbox)
    df = nrt.frame()
    if df is None:
        return {"available": False, "reason": nrt.error or "live feed is loading"}
    try:
        return cached({**store.nowcast(df, aoi), "fetched_at": nrt.fetched_at()}, 600)
    except NeedsData as e:
        return needs_data(e)


@app.get("/api/locate")
def locate(lon: float = Query(..., ge=-180, le=180), lat: float = Query(..., ge=-90, le=90)):
    cid = store.country_at(lon, lat)
    return {"country": cid, "name": store.meta[cid]["name"] if cid else None}


@app.post("/api/prepare")
def prepare(ids: list[str] = Body(...)):
    bad = [i for i in ids if i not in store.meta]
    if bad:
        raise HTTPException(404, f"unknown countries {bad}")
    if len(ids) > 12:
        raise HTTPException(400, "at most 12 countries per request")
    store.refresh()
    return {c: (jobs.submit(c) if c not in store.ready else {"state": "ready"}) for c in ids}


@app.get("/api/jobs")
def job_status():
    store.refresh()
    return JSONResponse(jobs.status(), headers={"Cache-Control": "no-cache"})


# ───────────────────────────── web app ─────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{v}}", VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/world.geojson")
def world():
    return FileResponse(RES / "world.geojson", media_type="application/geo+json",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/sw.js")
def service_worker():
    js = (STATIC / "sw.js").read_text(encoding="utf-8").replace("{{v}}", VERSION)
    return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


class ImmutableStatic(StaticFiles):
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        if r.status_code == 200:  # URLs carry ?v=<hash>, so they can be cached forever
            r.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return r


app.mount("/static", ImmutableStatic(directory=STATIC), name="static")


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"internal error: {exc.__class__.__name__}"})
