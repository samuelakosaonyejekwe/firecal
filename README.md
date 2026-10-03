# FireCal: Harmonized MODIS + VIIRS Burning Activity Calendar

**NASA Space Apps Challenge 2026: *Harmonization of MODIS and VIIRS Hot Spots***

**Live app: https://samuelakosaonyejekwe.github.io/firecal/**

Satellites have tracked active fires since 2000, but the record is split between MODIS
(Terra/Aqua, 1 km, 2000→) and VIIRS (S-NPP, 375 m, 2012→), which count fires differently.
FireCal harmonizes the two into **one consistent 24-year burning activity calendar for any
area on Earth**, so emergency responders, scientists and land managers can examine
historical fire patterns, unusual conditions and critical periods, and see how this week
compares with every past year.

## What it does

| Need (from the challenge) | FireCal feature |
|---|---|
| Any **selected area of interest** | Search, click or tap any of 208 countries (a tap on a phone first explains the map square, with Analyze buttons), analyze just the **~55 km area around any fire square** (click a square inside the country shown, Shift-click one anywhere, or the button on a phone), **draw a box anywhere** (cross-border boxes merge countries without double-counting), use **◎ Me** (geolocation), or type coordinates; every square on the map names its **nearest town** ("18 km NE of Ogbomosho · Oyo") |
| **Harmonize** MODIS + VIIRS | Per-area, per-month calibration on the 2012–present overlap, with three sensor eras and shrinkage to a worldwide prior; validated out of sample |
| **Burning activity calendar** | Month × year calendar (activity or anomaly σ), with a click-through to a day-by-day calendar for any year |
| **Historical fire patterns** | Seasonal profile (normal range vs any season), season timing, season totals, long-term trend |
| **Unusual conditions** | Months ≥ 2σ from normal (off-season noise filtered); season z-scores |
| **Critical periods** | Season opens / peaks / closes, highest-risk weeks, and today's position in the season |
| **Early warning** | The last 6 complete days of NASA near-real-time VIIRS detections vs the same dates in every year since 2001 (percentile, status); 30-day climatological outlook |
| **Responders / scientists / land managers** | "What this means" plain-language briefing written for each audience |
| Beyond the brief | Live global fire map, shareable links for every view, CSV export, printable report, JSON downloads (website) and an API with `/docs` (local server), installable app on computers, Android and iPhone/iPad (**Install** button: the browser's install prompt where it exists, step-by-step instructions on iOS, Mac Safari, Samsung Internet and Firefox), airplane mode (after one visit the app, world map, fonts and every area opened work offline; "Save all countries for offline" stores every calendar and map layer, about 14 MB), light/dark themes, works on phones; a content security policy, integrity-checked libraries and (local server) DNS-rebinding and cross-site protection |

## Two editions, one codebase

| | Website (GitHub Pages) | Local server |
|---|---|---|
| Address | https://samuelakosaonyejekwe.github.io/firecal/ | http://127.0.0.1:8765 |
| Countries | every country published to the `firecal-data` release | any country on demand: downloaded from the release if published, otherwise built from NASA (and published, with a `gh` login that may write to the repository) |
| Drawn boxes | analyzed in the browser from 0.1° fire tiles (up to 20° × 20°) | analyzed on the server (up to ~70° × 70°) |
| Live fires | NASA serves the feed from two servers that update independently; `app/feeds.py` takes the newest data from either and dates it by content (a server re-stamping an unchanged file doesn't count as new). A cloud watcher (`.github/workflows/live.yml`) checks NASA every 5 min around the clock and rebuilds the site when NASA posts new data, handing over to its own successor every ~5.5 h, so neither a laptop nor GitHub's best-effort schedule is needed; a second self-renewing chain, the guardian (`.github/workflows/guard.yml`, plain shell), checks every 10 min that the watcher runs and restarts it, while the watcher restarts the guardian, so neither depends on GitHub's schedule (which drops most scheduled runs); the schedules, every website build and the local server (`pipeline/keeper.py`) are extra restarters. Independently of GitHub, each visitor's browser asks NASA (a few hundred bytes, on opening and every 10 min) whether it has newer fires than the published copy; if it does (and GitHub hasn't published them within 15 min), the browser reads NASA's last-24-hours file (~6 MB) from the FIRMS mirror that allows browser access, joins it to the earlier days, and builds the same live data with the same rules (the 7-day file if the copy is over a day old), including the gas-flare mask and each country's recorded cells outside its border (`app/static/live.js`, parity-tested against `pipeline/live.py`), showing the published copy until it is ready; the app flags live data more than 6 h behind NASA | checks NASA every 10 min, downloads when the data changed (`app/nrt.py`) |

Both use the same harmonization: `app/analysis.py` (Python) and `app/static/engine.js` (browser)
are checked against each other by `tests/test_engine_parity.py` on every change.

## Keep everything in sync

```bash
.venv/bin/python pipeline/world.py            # all 208 countries, resumable
.venv/bin/python pipeline/world.py Chad       # or just some
.venv/bin/python pipeline/world.py --update   # also rebuild countries once NASA publishes a new yearly archive
.venv/bin/python pipeline/world.py --help     # all options
```

For each country it uses the cheapest source: this computer → the `firecal-data` release on GitHub
(a processed file of up to ~50 MB, about 1/55 the size of the raw NASA data) → NASA (downloaded,
processed, then published with your own `gh` login). If a country exists in both places but differs,
the newer copy wins. Publishing needs a `gh` login with write access to the repository; without one
(a fork, Docker, a fresh machine) published countries are still downloaded, over public HTTPS, and
nothing is published. It then asks GitHub to rebuild the website, so **your computer, the repository
and the website all end up with the same countries**. Raw NASA files are deleted as it goes
(keep ~5 GB free). Logic and tests: `pipeline/sync.py`, `tests/test_sync.py`.

Two things happen on their own as the world fills in: countries NASA has no usable archive for are
recorded once (`unavailable.json` in the release), skipped by later runs and explained in the app;
and once at least 20% more countries are available than the worldwide calibration prior was computed
from (and once more when every country is in), `pipeline/finalize.py` recomputes `app/resources/prior.json`, commits only that file under the
owner's name and pushes it, which rebuilds the website (it skips if the local copy has diverged).

## In the cloud (no laptop needed)

`.github/workflows/world.yml` runs the same sync on GitHub: 10 parallel workers (5 at a time) build
every country not yet published, publish it under the owner's name and rebuild the website; a
monthly run picks up NASA's new yearly archives, and a final step updates the calibration prior
when due. A worker that can't reach NASA stops early, and if any country is still missing the run
starts itself again on fresh machines (up to 3 attempts; `python pipeline/world.py --missing` lists
what is left). It needs one repository secret,
`FIRECAL_PUBLISH_TOKEN`: a fine-grained personal access token limited to this repository with
**Contents: read and write** and **Actions: read and write**. Start it from the Actions tab
("Build world" → Run workflow) or with `gh workflow run world.yml`.

## Run it locally

```bash
./start.sh        # sets up Python on first run, starts in the background → http://127.0.0.1:8765
./stop.sh         # stop it (server log: data/server.log)
```

Or run it in the foreground:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8765
```

That's all: any country is **fetched on demand** the first time someone opens it: from GitHub
if it is already published (seconds), otherwise from NASA (about 30 s for a small country, several
minutes for Brazil or DR Congo), then cached (and published, with a `gh` login that may write to the
repository). Needs Python 3.11+; `start.sh` is for Linux (or WSL) and reinstalls packages whenever
`requirements.txt` changes. The server answers to `127.0.0.1` and `localhost`; to open it under another
name (a LAN address, a domain), list it: `FIRECAL_ALLOWED_HOSTS=192.168.1.20 ./start.sh`.

Optional:

```bash
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest tests
.venv/bin/python pipeline/static_site.py --out site --build build   # build the website edition locally
docker build -t firecal . && docker run -p 8765:8765 -v firecal-data:/data firecal   # deploy anywhere
```

The Docker image runs as a non-root user with a health check; published countries come from the
public release and nothing is published (there is no `gh` login inside); set `FIRECAL_ALLOWED_HOSTS`
for host names other than `localhost`. On pushes to `main` and on pull requests, GitHub runs the tests
and builds and smoke-tests the image (`.github/workflows/ci.yml`). The website build streams through DuckDB with
a memory cap (`FIRECAL_BUILD_MEMORY`, default 2GB), so its memory stays flat at world scale.

## Harmonization method

1. **Clean.** FIRMS `type 0` (vegetation fire) only, so volcanoes, gas flares and industrial heat are dropped; MODIS confidence ≥ 30; VIIRS nominal/high.
2. **Common grid.** Both sensors are binned to 0.1° cells per day. A cell with ≥ 1 detection is a **fire cell-day**. This removes most of the pixel-size effect (one fire is many 375 m VIIRS pixels but one cell).
3. **Calibrate locally.** Over the overlap years, for the selected area:
   `k_area = (ΣV + Λ·k_world)/(ΣM + Λ)` and `k_month = (ΣV_m + λ·k_area)/(ΣM_m + λ)`,
   so data-rich areas get their own seasonal ratios while small or quiet areas fall back smoothly on the worldwide ratio.
   `k_world` is **fixed and versioned** in `app/resources/prior.json` (pooled over the countries listed
   there; regenerate with `pipeline/prior.py` and commit), so an area's results never depend on which
   other countries happen to be loaded. All parameters live in `app/constants.py`.
4. **Three eras.** Terra-only (Nov 2000 – Jul 2002, half the overpasses) gets its own factor; Terra+Aqua (→ Jan 2012) is scaled by `k_month`; VIIRS is the reference afterwards. Terra's orbit drift is excluded from its fit from 2022, where the data show it: the worldwide VIIRS/Terra ratio was 6.73 ± 0.25 in 2012–2019 and normal in 2020–2021, then jumped to 7.62 in 2022 (+3.6 σ) as Terra's share of MODIS fires fell.
5. **VIIRS outages.** On 45 days since 2012 (e.g. 27 Jul – 10 Aug 2022 and five episodes in 2024) S-NPP VIIRS recorded almost nothing worldwide while MODIS saw fires as usual. These are found once over all countries (`pipeline/prior.py`: VIIRS below a quarter of its usual ratio to MODIS) and listed in `prior.json`; on those days the record is filled from calibrated MODIS, as before 2012, and they are left out of every fit and test, instead of reading as "no fire" (false anomalies and a low calibration).
6. **Validate.** Leave-one-year-out cross-validation predicts each VIIRS year from MODIS alone, plus a VIIRS-independent check (Terra-only vs Terra+Aqua, 2003–2011). The held-out year still counts towards the worldwide prior `k_world` (pooled over all years), which matters only where an area leans on that prior; the monthly R² is in-sample.

Results (median out-of-sample annual error, worldwide prior k_world = 2.665 from all 208 countries):
DR Congo **1.5%** (monthly R² 0.99), India **1.8%**, Nigeria **2.0%** (R² 0.99), Brazil 3.4%, Russia 3.5%,
Ghana 3.7%, Australia 4.0%, Togo 5.0%, United States 6.8%. Areas with few fires (under 3,000 VIIRS
fire cell-days in 2012+), a weak monthly fit (R² < 0.5) or a test error above 15% are flagged
**"indicative only"** with the reason, e.g. Cyprus (≈110 fire cell-days a year, 14.4%) and Germany (28.1%).
The test suite in `tests/` checks that the method recovers a known ratio, removes the artificial 2012 jump,
that the browser and Python engines agree, and that the website's tiles reproduce the server exactly.

## Architecture

```
NASA FIRMS yearly country CSVs ──► pipeline/fetch.py ──► pipeline/build.py ──► data/countries/<id>/grid_daily.parquet
NASA FIRMS 7-day NRT feed ──► app/nrt.py (server) / pipeline/live.py (website)
                          app/analysis.py (DuckDB + pandas: harmonize, calendar, seasons, anomalies, nowcast)
                          app/main.py (FastAPI: gzip, HTTP caching, on-demand job queue, /docs)
                          app/static (MapLibre GL + a slim custom ECharts bundle, engine.js, geo.js, live.js, data.js, service worker)

pipeline/world.py + pipeline/sync.py ──► GitHub release "firecal-data" ──► .github/workflows/pages.yml
                      ──► tests ──► pipeline/static_site.py + pipeline/live.py ──► GitHub Pages
pipeline/keeper.py (cloud watcher and local server: rebuild the website when it falls behind NASA; restart the watcher)
pipeline/boundaries.py (Natural Earth → app/resources), pipeline/places.py (GeoNames → app/resources/places), pipeline/prior.py (→ app/resources/prior.json)
```

API: `/api/calendar?country=Kenya` or `?bbox=w,s,e,n`, `/api/nowcast`, `/api/grid`, `/api/live`,
`/api/locate?lon=…&lat=…`, `/api/meta`, `/api/prepare`, `/api/jobs`, `/api/health`, and the place-name tiles
`/places/<tile>.json`; interactive docs at `/docs`.

## Data and limits

- NASA FIRMS: MODIS Collection 6.1 (MCD14ML), VIIRS S-NPP 375 m (VNP14IMG), VIIRS S-NPP NRT. Boundaries: Natural Earth. Place names on the map card: GeoNames towns of 1 000+ people (CC BY 4.0).
- The yearly archive currently ends 31 Dec 2024 (FIRMS publishes each year's archive later). A year counts once FIRMS has published it for both sensors, and the app shows it once every country has been rebuilt with it (each country's `built.json` records its last archive year), so a partly updated world never shows a year of false zeros. The live panel covers the last 6 complete days; the gap from Jan 2025 to last week is not filled (it could be, with a free FIRMS MAP_KEY and the FIRMS area API, but that is not implemented).
- Near-real-time detections are provisional. Static-source masking uses each country's archive, so it applies once the country is loaded.
- Boxes must not cross the 180° meridian. Both editions decide which countries a box touches from the same border file (`app/resources/shapes.geojson`, Natural Earth 1:50m snapped to 0.0001°); `tests/test_geo_parity.py` checks the browser and server agree.
- Map layers show VIIRS-equivalent fire days per 0.1° cell (whole-country ratios, not month by month); multi-year layers are the mean per year over the years that hold that month (or the record's length).
- Place names come from the nearest notable town (bigger towns count as a little closer), the same answer as searching every town on Earth; only places with no town within 250 km at all (open ocean, ice sheets) get none.
- Fire cell-days measure *how widespread* burning is, not burned area or emissions.
