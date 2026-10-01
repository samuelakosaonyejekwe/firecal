# FireCal: Harmonized MODIS + VIIRS Burning Activity Calendar

**NASA Space Apps Challenge 2026: *Harmonization of MODIS and VIIRS Hot Spots***

Satellites have tracked active fires since 2000, but the record is split between MODIS
(Terra/Aqua, 1 km, 2000→) and VIIRS (S-NPP, 375 m, 2012→), which count fires differently.
FireCal harmonizes the two into **one consistent 24-year burning activity calendar for any
area on Earth**, so emergency responders, scientists and land managers can examine
historical fire patterns, unusual conditions and critical periods, and see how this week
compares with every past year.

## What it does

| Need (from the challenge) | FireCal feature |
|---|---|
| Any **selected area of interest** | Search or click any of 208 countries, **draw a box anywhere** (cross-border boxes merge countries without double-counting), use **◎ Me** (geolocation), or type coordinates |
| **Harmonize** MODIS + VIIRS | Per-area, per-month calibration on the 2012–present overlap, with three sensor eras and shrinkage to a worldwide prior; validated out of sample |
| **Burning activity calendar** | Month × year calendar (activity or anomaly σ), with a click-through to a day-by-day calendar for any year |
| **Historical fire patterns** | Seasonal profile (normal range vs any season), season timing, season totals, long-term trend |
| **Unusual conditions** | Months ≥ 2σ from normal (off-season noise filtered); season z-scores |
| **Critical periods** | Season opens / peaks / closes, highest-risk weeks, and today's position in the season |
| **Early warning** | Last week of NASA near-real-time VIIRS detections vs the same dates in every year since 2001 (percentile, status); 30-day climatological outlook |
| **Responders / scientists / land managers** | "What this means" plain-language briefing written for each audience |
| Beyond the brief | Live global fire map, shareable links for every view, CSV export, printable report, open JSON API (`/docs`), installable offline-capable app, light/dark themes, works on phones |

## Run it

```bash
./start.sh        # sets up Python on first run, starts in the background → http://127.0.0.1:8765
./stop.sh         # stop it (server log: data/server.log)
```

Or run it in the foreground:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8765
```

That's all: any country is **downloaded and harmonized on demand** the first time someone
opens it (about 30 s for a small country, a few minutes for Brazil or DR Congo), then cached.

Optional:

```bash
.venv/bin/python pipeline/world.py                  # preload all 208 countries (~30 GB download, ~1 GB kept)
.venv/bin/python pipeline/world.py Chad Kenya        # or just some
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest tests
docker build -t firecal . && docker run -p 8765:8765 -v firecal-data:/data firecal   # deploy anywhere
```

## Harmonization method

1. **Clean.** FIRMS `type 0` (vegetation fire) only, so volcanoes, gas flares and industrial heat are dropped; MODIS confidence ≥ 30; VIIRS nominal/high.
2. **Common grid.** Both sensors are binned to 0.1° cells per day. A cell with ≥ 1 detection is a **fire cell-day**. This removes most of the pixel-size effect (one fire is many 375 m VIIRS pixels but one cell).
3. **Calibrate locally.** Over the overlap years, for the selected area:
   `k_area = (ΣV + Λ·k_world)/(ΣM + Λ)` and `k_month = (ΣV_m + λ·k_area)/(ΣM_m + λ)`,
   so data-rich areas get their own seasonal ratios while small or quiet areas fall back smoothly on the worldwide ratio.
4. **Three eras.** Terra-only (Nov 2000 – Jul 2002, half the overpasses) gets its own factor; Terra+Aqua (→ Jan 2012) is scaled by `k_month`; VIIRS is the reference afterwards. Terra's post-2022 orbit drift is excluded from its fit.
5. **Validate.** Leave-one-year-out cross-validation predicts each VIIRS year from MODIS alone, plus a VIIRS-independent check (Terra-only vs Terra+Aqua, 2003–2011).

Results (median out-of-sample annual error): Nigeria **2.1%** (monthly R² 0.99), Ghana 3.7%,
Brazil 3.9%. Sparse areas such as Cyprus (≈100 fire cell-days a year) are flagged
"indicative only". A synthetic test in `tests/` confirms the method recovers a known ratio
and removes the artificial 2012 jump.

## Architecture

```
NASA FIRMS yearly country CSVs ──► pipeline/fetch.py ──► pipeline/build.py ──► data/countries/<id>/grid_daily.parquet
NASA FIRMS 7-day NRT feed (refreshed every 3 h) ──► app/nrt.py
                          app/analysis.py (DuckDB + pandas: harmonize, calendar, seasons, anomalies, nowcast)
                          app/main.py (FastAPI: gzip, HTTP caching, on-demand job queue, /docs)
                          app/static (MapLibre GL + ECharts, no build step, service worker)
```

API: `/api/calendar?country=Kenya` or `?bbox=w,s,e,n`, `/api/nowcast`, `/api/grid`, `/api/live`,
`/api/meta`, `/api/prepare`, `/api/jobs`, `/api/health`; interactive docs at `/docs`.

## Data and limits

- NASA FIRMS: MODIS Collection 6.1 (MCD14ML), VIIRS S-NPP 375 m (VNP14IMG), VIIRS S-NPP NRT. Boundaries: Natural Earth.
- The yearly archive currently ends 31 Dec 2024 (FIRMS publishes each year's archive later). The live panel covers the current week; 2025 to last week can be filled with a FIRMS MAP_KEY via the area API.
- Near-real-time detections are provisional. Static-source masking uses each country's archive, so it applies once the country is loaded.
- Fire cell-days measure *how widespread* burning is, not burned area or emissions.
