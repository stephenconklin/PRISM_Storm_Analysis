# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# PRISM Storm Analysis

Tools for programmatically downloading Oregon State University PRISM gridded
climate data, clipping it to any US watershed, and summarizing storm events.
Keep code, CLI defaults and docs watershed-agnostic; specific basins appear only as examples.

First use case (example only): the late-October 2026 San Juan River floods (remnants of
Hurricane Trudy; rain began ~Oct 21, San Juan at Pagosa Springs crested
15.02 ft ~Oct 22–23, showers cleared by ~Oct 26; another storm Oct 30–Nov 1).
That run covered HUC 1408, ppt and tmean for PRISM days Oct 20–31.

## Layout
- `scripts/pipeline.py` — `init` (writes `storm.toml`), `run` (all steps in order as subprocesses; resumes from
  `<out>/pipeline_state.json` fingerprints; network steps soft, `ppt`/`totals` hard), `status`, `update` (refresh +
  rerun + what changed). Step arguments are computed when the step starts (earlier steps make inputs)
- `scripts/config.py` — per-folder settings `<out>/storm.toml` (TOML: tomllib, tomli on 3.10); `config.parse_args(ap)`
  fills defaults from it (role `download` for prism_watershed, `event` for the rest; adds `--event`); flags win.
  An event may set its own `after_days`; it applies when `--event` or `--start`/`--end` select that event
- `scripts/runtime.py` — stdlib only: exit codes (0 ok, 1 QC, 2 input, 3 incomplete, 4 network, 5 error, 130
  interrupted), `die()`, `run_main()` (records failed/interrupted runs in the manifest), `attach()` (folder `.lock`,
  log in `<out>/logs/`), `write_atomic`/`atomic_open`/`atomic_path` (`.part` + rename), `confirm()` (only at a
  terminal), `windowed()`/`window_json()` for per-window validation copies
- `scripts/check_endpoints.py` — one light probe per external service; `STEP_SERVICES` maps pipeline steps to them
- `.claude/skills/` — `storm-run`, `storm-update`, `check-endpoints`, `report-check`
- `scripts/find_huc.py` — look up WBD HUC codes by name (network only)
- `scripts/prism_watershed.py` — download → unzip → clip → daily CSV + event-total raster + daily NetCDF cube
- `scripts/storm_total.py` — `build_cube`, ellipsoidal `cell_area_m2`, `prism_day_files` (national grid lookup in
  `extracted/`), and a CLI that totals depth/volume for any date range from the cube, with releases, revision
  change (from the release history) and boundary sensitivity (centers / area-weighted / touched, from the national
  grids). Volumes use per-row GRS80 cell areas on the native grid; don't reproject or resample. NetCDF outputs are CF-1.10
  (`cf_time`, `cf_globals`, `VAR_CF`): time = 00:00 UTC on the day label (the midpoint) with `time_bnds` 12:00–12:00 UTC;
  keep it there, since date selection relies on it.
- `scripts/uncertainty.py` — pure components (gauges, boundary, revisions, products) and the bounding range
  (sum of lows/highs) for `storm_total.py`; writes `<out>/ppt_<res>_uncertainty_<start>_<end>.json`, which the QGIS
  basin label reads. Method and judgments in `docs/UNCERTAINTY.md`; keep it in step with the code
- `scripts/qc.py` — pure QC check functions (pass/warn/fail/skip) run at the end of `prism_watershed.py` and
  `storm_total.py`; any fail → exit status 1 after outputs are written; `--qc-strict` makes warnings fail
- `scripts/manifest.py` — appends one entry per run of any script to `<out>/run_manifest.json` (`runs` list,
  written atomically): command, status, log, git commit, versions, basin source, per-day URL/SHA-256/release/info.txt
  fields, outputs with SHA-256, QC, and `input_releases` (cube releases for the entry's window; `status` uses it)
- `scripts/observations.py` — all network access for validation (RCC-ACIS stations, USGS Water Data API, NLDI,
  National Inventory of Dams, WBD units, NRCS AWDB SNOTEL); `_get` retries 429/503 and raises `RateLimited` on long waits
- `scripts/validate_stations.py` — gauge vs PRISM comparison; pure functions for ACIS parsing, day alignment,
  event totals and stats; writes `validation/validation.gpkg` with styles from `styles/*.qml`
- `scripts/validate_runoff.py` — gauge-basin precipitation volume vs 15-minute streamflow, runoff ratio, for
  `--gauge` sites or `--all-gauges`; gauge-basin precipitation from the national grids in `extracted/` (not the
  clipped cube, so NLDI basins may extend past the study basin); regulation class from NID storage over the whole gauge basin; per-gauge results cached
  per window in `validation/runoff_<site>_<start>_<end>.json` (`result_paths`/`save_result`; `runoff_<site>.json`
  is the latest run) so rate-limited runs resume. Cache rules (`load_cached`): a change in
  `PRECIP_KEYS` (res, max_missing_pct, releases, rain/snow, tmean releases) recomputes precipitation offline;
  results or skips whose flow window was cut short by the run time are refetched (`flow_stale`); transient
  request errors are never cached. `--gauge` updates only its own gauges in the layers
- `scripts/validate_snow.py` — rain/snow split vs SNOTEL SWE gain (observed, station-data split, PRISM split) per
  station; `validation/snotel.csv`, `snotel_summary.json`, layer `snotel`; pure functions `snotel_window`, `prism_split`
- `scripts/storm_context.py` — storm vs PRISM 1991–2020 normals (prorated monthly, or `--daily-normals`); normals
  cached in `<out>/normals_zips/`; `ppt_<res>_normals_<start>_<end>.json` feeds the report tile; `--pct-tif` map
- `scripts/subbasin_totals.py` — basin and subbasin (HUC10/12) depth and volume from the cube (`zone_totals` in
  `storm_total.py`) to `<out>/totals.gpkg` (`basin_total`, `subbasins`); WBD units via `observations.wbd_units`
- `scripts/build_qgis_project.py` — builds `<out>/QGIS/<name>.qgz` from a folder's outputs (found by standard
  names; one resolution, 800m preferred). `plan_events` (pure, sqlite3 for GeoPackages) picks the storm windows
  (storm.toml events, else uncertainty files, else the legacy primary total) and each window's rasters (by the
  `_<start>_<end>` at the end of the name) and layers (`<layer>_<span>`, else the plain layer if made for that
  window); one group and theme set per window, shared color scales per kind; extra tifs mapped only if
  `tif_kind` recognizes the name. Validation and subbasin scripts write window-named layers via
  `write_layer(..., window=(start, end))`; keep new per-window layers in that scheme. Errors are caught in `build()` and the traceback freed before `exitQgis()`. Runs under QGIS's Python; relaunches itself there (newest QGIS 3.x app by plist version, so projects open in
  QGIS 3 and 4; else newest QGIS; or `--qgis-python`/`QGIS_PYTHON`) with Anaconda's PROJ/GDAL/PDAL env vars cleared. Pure helpers are testable without
  QGIS. In GDAL Python keep the dataset referenced while using its band (a temporary dataset frees the band).
- `scripts/report.py` — `<out>/report/storm_report_<start>_<end>.html` (self-contained: hand-built SVG charts, CSS
  light/dark tokens, no external resources) and `report/figures/*.svg`. Colors are the dataviz skill's validated
  slots 1–3; check charts by screenshot (headless Chrome) after layout changes. Sections skip with a note when
  their inputs are missing or for another window
- PyQGIS returns a NULL object (not None) for empty fields: filter with `is_number()`. Build inside an inner
  function so layer objects are freed before `exitQgis()` (else a segfault on exit).
- `styles/` — QGIS QML styles embedded into GeoPackages (`layer_styles` table) by `write_default_style`
- `docs/ROADMAP.md` — planned features with approach and done-criteria; read it before starting new feature work
  and update it as items land
- `tests/` — offline only (synthetic rasters; requests go to a closed local port via `OFFLINE` env). `test_offline.py`
  runs `prism_watershed.py` end to end; `test_pipeline.py` runs the pipeline, interrupts it with Ctrl-C and resumes;
  `test_build_qgis_project.py` builds a real project when QGIS is installed (skipped otherwise, e.g. in CI:
  `.github/workflows/tests.yml` runs the suite from `environment.yml` on every push/PR)
- `data/` — all downloads and outputs (git-ignored; never commit rasters)

`prism_watershed.py` requires `--start`, `--end`, and one of `--huc`/`--boundary` (or a `storm.toml` in `--out`). `--cache`/`PRISM_CACHE` shares national zips between folders (hard links). `--var tmean` (etc.) can share a
`ppt` folder: own cube and `<var>_<res>_daily_basin_summary.csv`, no event total (ppt at 4km also gets the
`<var>_<res>_` summary name; 800m ppt keeps `daily_basin_summary.csv`). `--boundary` runs save `boundary.geojson`,
which later steps use like `wbd_huc*.geojson`. Basin means are area-weighted everywhere. `storm_total.py` splits rain/snow
from the `tmean` cube (`snow_fraction`, `rain_snow`); `validate_runoff.py` adds rain-only ratios from national
`tmean` grids in `extracted/`. Discovery globs that mean precipitation must say `ppt_` (e.g. QGIS `discover`).
Outputs (default `--out data/huc<code>` or `data/<boundary stem>`): `raw_zips/`, `extracted/`, `clipped/<var>_<res>_YYYYMMDD_clip.tif`,
`daily_basin_summary.csv` (every day in the folder; a run replaces its own days), `<var>_<res>_total_<start>_<end>.tif`, `<var>_<res>_daily_clip.nc` (rebuilt each run
from every tif in `clipped/`, so one `--out` folder = one basin), `<var>_<res>_release_history.csv`. `--end` is capped at yesterday.

Releases: the release is read from the zip's info.txt; clipped tifs carry it as tag `PRISM_DATASET_RELEASE_NUMBER`
and the cube as `release` (0 unknown). `--refresh` re-downloads days whose release PRISM's releaseDate service reports as newer
(`available_releases`; if unreachable, zips at least `--refresh-age` days old, file mtime = last checked) and moves a superseded zip to
`raw_zips/releases/<stem>_r<n>.zip`. `extract_grid` re-extracts when `extracted/` holds another release. A newer
release replaces the day everywhere; older values live only in the history CSV and archived zips. Other event-total
tifs covering a revised day are reported as out of date (`stale_totals`).
`validate_runoff.py` run keys include releases; a release-only change recomputes precipitation without network.

The offline test runs the script as a subprocess with zips pre-seeded in `raw_zips/` and a local
`--boundary`, so the cache check skips every network call. Zips are cached under an exact name from
`zip_name()`: `prism_<var>_<region>_<rescode>_<YYYYMMDD>[_<format>].zip` (rescode `30s` 800 m, `25m` 4 km;
`RES_CODES` in `storm_total.py`), which equals PRISM's own name for the default COG. Never match cache or
extracted files without the resolution code. If you change cache naming or lookup, keep the test working.

## Setup
```
conda env create -f environment.yml && conda activate prism-storm-analysis   # preferred (all conda-forge)
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```
Keep `environment.yml` and `requirements.txt` in sync when adding dependencies.
To run non-interactively without activating, use `conda run -n prism-storm-analysis python ...`.
The "numpy.ndarray size changed" RuntimeWarning in tests comes from netCDF4 and is harmless.
Tests set `PRISM_STORM_RETRY_WAIT=0` so retries of the (deliberately failing) offline requests don't wait.

## Run
```
python scripts/pipeline.py init [--find NAME | --huc CODE | --boundary FILE] [--start ... --end ...] [--run]
python scripts/pipeline.py run --out DIR [--dry-run] [--from STEP] [--only STEP ...] [--event NAME]
python scripts/pipeline.py status --out DIR [--offline]
python scripts/pipeline.py update --out DIR [--check --notify]
python scripts/check_endpoints.py [--only prism_releases usgs ...]
python scripts/find_huc.py animas [--digits 4 8] [--state CO]
python scripts/prism_watershed.py --huc 14080104 --start 2026-10-20 --end 2026-10-27 [--out DIR]
python scripts/prism_watershed.py --boundary basin.gpkg --start ... --end ...
python scripts/prism_watershed.py --huc ... --start ... --end ... --out DIR --refresh [--target-release 8] [--refresh-age 7]
python scripts/storm_total.py --out DIR [--start ... --end ...] [--tif depth_mm.tif] [--volume-tif m3.tif] [--volume-nc m3.nc]
python scripts/validate_stations.py --out DIR [--start ... --end ...] [--buffer-km 10]
python scripts/validate_runoff.py --out DIR --all-gauges [--after-days 7] [--refresh]
python scripts/validate_runoff.py --out DIR --gauge 09342500 09361500
python scripts/validate_snow.py --out DIR [--start ... --end ...] [--all-snow-c 0 --all-rain-c 3]
python scripts/storm_context.py --out DIR [--start ... --end ...] [--daily-normals] [--pct-tif DIR/pct_of_normal.tif]
python scripts/subbasin_totals.py --out DIR [--digits 10|12] [--start ... --end ...]
python scripts/build_qgis_project.py --out DIR [--name NAME] [--qgis-python PATH]
python scripts/report.py --out DIR [--start ... --end ...] [--gauges SITE ...]
python -m pytest tests -q
python -m pytest tests/test_qc.py -q              # one file
python -m pytest tests/test_offline.py -k refresh  # tests matching a name
```
There is no linter or build step configured.
On this Mac the shell sets `PROJ_DATA=/opt/anaconda3/share/proj`, an old `proj.db` that breaks pip
rasterio ("EPSG code is unknown … comes from another PROJ installation"). Use the activated
`prism-storm-analysis` env (or `conda run -n prism-storm-analysis`), `.venv`, or prefix with `env -u PROJ_DATA`.

`services.nacse.org` chains to emSign Root CA - G1, which certifi's bundle lacks (CERTIFICATE_VERIFY_FAILED).
The script calls `truststore.inject_into_ssl()` to use the OS trust store; never use `verify=False`.

## PRISM web service facts (post 1 Oct 2025 — older docs/blogs show the retired syntax)
- URL: `https://services.nacse.org/prism/data/get/<region>/<res>/<var>/<YYYYMMDD|YYYYMM|YYYY>`
  - region `us` (hi/ak/pr in development); res `800m` or `4km`; var `ppt tmin tmax tmean tdmean vpdmin vpdmax`
  - Optional `?format=nc|bil|asc`; default is Cloud Optimized GeoTIFF, delivered zipped.
  - Retired syntax: `services.nacse.org/prism/data/public/4km/...` — do not use.
- 800m time series has been free since 27 Mar 2025. Daily data from 1981-01-01.
- **Hard limit: the same file can be downloaded only twice per 24 h per IP.** Always cache zips
  in `data/.../raw_zips` and never re-request one that exists. Pause ~2 s between requests.
- Stability is no longer in filenames or labeled early/provisional/stable. The zip's `*.info.txt` has
  `PRISM_DATASET_RELEASE_NUMBER` (1–8): daily grids are released at D+1, D+5, then ~monthly for 6 months.
  Recent days are remodeled for ~6 months, so re-pull later for final numbers with `--refresh`.
- Release numbers without downloading (checked 1 Nov 2026): `.../get/releaseDate/<region>/<res>/<var>/<start>/<end>?json=true`
  returns rows [data date, release date, element, grid count = release, URL]; `.../get/gridCount/<region>/<var>/<date>`
  returns one integer. These don't count toward the download limit. 30-year normals are NOT on the web service:
  `https://data.prism.oregonstate.edu/normals/<region>/<res>/<var>/<monthly|daily>/prism_<var>_<region>_<code>_2020MM[DD]_avg_30y.zip`
  (1991–2020; ~58 MB monthly, ~67 MB daily at 800 m; anonymous FTP at prism.oregonstate.edu has the same tree).
- Filenames use resolution codes: `30s` = 800m, `25m` = 4km, `15s` = 400m. Native CRS NAD83 (EPSG:4269), units mm.
- A PRISM "day" ends at 12:00 UTC, so pad event windows by a day on each side.

## Watershed boundaries
USGS WBD REST: `https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/<layer>/query`
with layer = HUC digits / 2 (1→HUC2 … 8→HUC16), field `huc2`…`huc16`, plus `name`, `states`, `areasqkm`.
Example HUCs: 1408 San Juan basin; 14080101 Upper San Juan; 14080104 Animas.

## Validation data facts (checked 31 Oct 2026; AWDB 1 Nov)
- RCC-ACIS `MultiStnData` (POST JSON, bbox) covers COOP, CoCoRaHS, ASOS and SNOTEL (GHCN ids `USS…`).
  Values in inches; `T` trace, `M` missing, `S` part of a later `…A` accumulated total.
  Observation hour: 1–24 local standard time (24 = midnight); **0 or -1 mean not recorded**, not midnight.
  SNOTEL always reports 0 (treated as midnight). SNOTEL lags a few days in ACIS.
- `*.stn.csv` in PRISM zips lists input stations (location, network) but **no observed values**.
- USGS Water Data API (`api.waterdata.usgs.gov/ogcapi/v0`) replaces NWIS Water Services. Daily values
  can be null for a whole day (qualifier `EQUIP`) while the 15-minute `continuous` record covers most of it,
  so runoff uses the 15-minute record. NLDI basin works; `splitCatchment=true` returned errors.
- Example gauges: 09342500 San Juan R. at Pagosa Springs; 09361500 Animas R. at Durango.
- **USGS Water Data API rate limit: 1,000 requests/hour/IP** (429 + Retry-After, can be ~20 min). Don't probe it
  repeatedly while testing; reuse saved results. `USGS_API_KEY` (api.data.gov key, `X-Api-Key` header) works
  and gets its own 1,000/hour allowance, separate from the per-IP one. Never write the key into repo files.
- Gauge discovery: `time-series-metadata` with `bbox`, `parameter_code=00060`, `computation_period_identifier=Points`;
  site metadata in bulk from `monitoring-locations` (`bbox`, `site_type_code=ST`).
- SNOTEL: NRCS AWDB REST (`wcc.sc.egov.usda.gov/awdbRestApi/services/v1`, no key) `stations` (`*:*:SNTL`, elevation
  in ft) and `data` (WTEQ, PREC, PRCP, TAVG; in, degF). Days are PST calendar days, WTEQ/PREC end-of-day:
  AWDB day D = PRISM day D+1 (lagged correlation 0.95–0.998). ACIS SNOTEL dates align the same way.
- Dams: NID ArcGIS service (`NORMAL_STORAGE`, `NID_STORAGE` in acre-ft). Drop `IS_ASSOCIATED_STRUCTURE='Yes'`
  (dikes repeat the reservoir's storage) and dedupe same-storage entries by base name or <2 km (e.g. Narraguinnep).
  NLDI `ref_dams` lacks storage and missed Navajo and Vallecito; NLDI `GRAND` returned nothing here.
  `DRAINAGE_AREA` (mi², checked 1 Nov 2026; null for some dams) caps each dam's effective storage at the storm
  depth over its own drainage area (`regulation_from`); the class uses effective storage. NIDIDs are not unique
  (CO00895 is both Terminal/Electra Lake and Stagecoach), so key dams by location, not NIDID.

## Conventions
- Python 3.10+, standard argparse CLIs, no notebooks in `scripts/`.
- Every script: `runtime.add_common_args(ap)`, `a = config.parse_args(ap)`, `run = manifest.start_run(...)`,
  `runtime.attach(out, run, a)`, stop with `die(msg, code)` (not `sys.exit(msg)`), end with
  `runtime.run_main(main)`. Write outputs with the runtime atomic helpers. Ask only via `runtime.confirm`.
- Validation CSV/JSON outputs are also kept as `<name>_<start>_<end>.<ext>` (`runtime.keep_window_copy`); readers
  use `runtime.window_json` / the windowed file so a folder can hold several events.
- `conda run` ignores stdin heredocs: put Python in a file to run it with `conda run`.
- Keep network code separate from raster processing so processing stays testable offline.
- Check the PRISM and USGS endpoints before relying on them. Endpoint changes break this project more than anything else.
