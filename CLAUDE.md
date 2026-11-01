# PRISM Data Wrangler

Tools for programmatically downloading Oregon State University PRISM gridded
climate data, clipping it to any US watershed, and summarizing storm events.
Keep code, CLI defaults and docs watershed-agnostic; specific basins appear only as examples.

First use case (example only): the late-October 2026 San Juan River floods (remnants of
Hurricane Trudy; rain began ~Oct 21, San Juan at Pagosa Springs crested
15.02 ft ~Oct 22–23, showers cleared by ~Oct 26; another storm Oct 30–Nov 1).
That run's outputs (HUC 1408, PRISM days Oct 20–27) are in
`/Volumes/ConklinGeospatialData/Data/PRISM_data/San_Juan_Watershed`.

## Layout
- `scripts/find_huc.py` — look up WBD HUC codes by name (network only)
- `scripts/prism_watershed.py` — download → unzip → clip → daily CSV + event-total raster + daily NetCDF cube
- `scripts/storm_total.py` — `build_cube`, ellipsoidal `cell_area_m2`, `prism_day_files` (national grid lookup in
  `extracted/`), and a CLI that totals depth/volume for any date range from the cube, with releases, revision
  change (from the release history) and boundary sensitivity (centers / area-weighted / touched, from the national
  grids). Volumes use per-row GRS80 cell areas on the native grid; don't reproject or resample.
- `scripts/qc.py` — pure QC check functions (pass/warn/fail/skip) run at the end of `prism_watershed.py` and
  `storm_total.py`; any fail → exit status 1 after outputs are written; `--qc-strict` makes warnings fail
- `scripts/manifest.py` — appends one entry per run to `<out>/run_manifest.json` (`runs` list): command, git
  commit, versions, basin source, per-day URL/SHA-256/release/info.txt fields, outputs with SHA-256, QC
- `scripts/observations.py` — all network access for validation (RCC-ACIS stations, USGS Water Data API, NLDI,
  National Inventory of Dams); `_get` retries 429/503 and raises `RateLimited` on long waits
- `scripts/validate_stations.py` — gauge vs PRISM comparison; pure functions for ACIS parsing, day alignment,
  event totals and stats; writes `validation/validation.gpkg` with styles from `styles/*.qml`
- `scripts/validate_runoff.py` — gauge-basin precipitation volume vs 15-minute streamflow, runoff ratio, for
  `--gauge` sites or `--all-gauges`; gauge-basin precipitation from the national grids in `extracted/` (not the
  clipped cube, so NLDI basins may extend past the study basin); regulation class from upstream NID storage depth; per-gauge results cached
  in `validation/runoff_<site>.json` (keyed by window settings) so rate-limited runs resume
- `scripts/validate_snow.py` — rain/snow split vs SNOTEL SWE gain (observed, station-data split, PRISM split) per
  station; `validation/snotel.csv`, `snotel_summary.json`, layer `snotel`; pure functions `snotel_window`, `prism_split`
- `scripts/subbasin_totals.py` — basin and subbasin (HUC10/12) depth and volume from the cube (`zone_totals` in
  `storm_total.py`) to `<out>/totals.gpkg` (`basin_total`, `subbasins`); WBD units via `observations.wbd_units`
- `scripts/build_qgis_project.py` — builds `<out>/QGIS/<name>.qgz` from a folder's outputs (found by standard
  names). Runs under QGIS's Python; relaunches itself there (newest QGIS 3.x app by plist version, so projects open in
  QGIS 3 and 4; else newest QGIS; or `--qgis-python`/`QGIS_PYTHON`) with Anaconda's PROJ/GDAL/PDAL env vars cleared. Pure helpers are testable without
  QGIS. In GDAL Python keep the dataset referenced while using its band (a temporary dataset frees the band).
- PyQGIS returns a NULL object (not None) for empty fields: filter with `is_number()`. Build inside an inner
  function so layer objects are freed before `exitQgis()` (else a segfault on exit).
- `styles/` — QGIS QML styles embedded into GeoPackages (`layer_styles` table) by `write_default_style`
- `docs/ROADMAP.md` — planned features with approach and done-criteria; read it before starting new feature work
  and update it as items land
- `tests/test_offline.py` — offline test using synthetic rasters (no network); `tests/test_qc.py` — QC and manifest
- `data/` — all downloads and outputs (git-ignored; never commit rasters)

`prism_watershed.py` requires `--start`, `--end`, and one of `--huc`/`--boundary`. `--var tmean` (etc.) can share a
`ppt` folder: own cube and `<var>_<res>_daily_basin_summary.csv`, no event total. `storm_total.py` splits rain/snow
from the `tmean` cube (`snow_fraction`, `rain_snow`); `validate_runoff.py` adds rain-only ratios from national
`tmean` grids in `extracted/`. Discovery globs that mean precipitation must say `ppt_` (e.g. QGIS `discover`).
Outputs (default `--out data/huc<code>` or `data/<boundary stem>`): `raw_zips/`, `extracted/`, `clipped/<var>_<res>_YYYYMMDD_clip.tif`,
`daily_basin_summary.csv` (every day in the folder; a run replaces its own days), `<var>_<res>_total_<start>_<end>.tif`, `<var>_<res>_daily_clip.nc` (rebuilt each run
from every tif in `clipped/`, so one `--out` folder = one basin), `<var>_<res>_release_history.csv`. `--end` is capped at yesterday.

Releases: the release is read from the zip's info.txt; clipped tifs carry it as tag `PRISM_DATASET_RELEASE_NUMBER`
and the cube as `release` (0 unknown). `--refresh` re-downloads days below `--target-release` whose zip is at
least `--refresh-age` days old (file mtime = last checked) and moves a superseded zip to
`raw_zips/releases/<stem>_r<n>.zip`. `extract_grid` re-extracts when `extracted/` holds another release. A newer
release replaces the day everywhere; older values live only in the history CSV and archived zips.
`validate_runoff.py` run keys include releases; a release-only change recomputes precipitation without network.

The offline test runs the script as a subprocess with zips pre-seeded in `raw_zips/` and a local
`--boundary`, so the cache check skips every network call. Zips are cached under an exact name from
`zip_name()`: `prism_<var>_<region>_<rescode>_<YYYYMMDD>[_<format>].zip` (rescode `30s` 800 m, `25m` 4 km;
`RES_CODES` in `storm_total.py`), which equals PRISM's own name for the default COG. Never match cache or
extracted files without the resolution code. If you change cache naming or lookup, keep the test working.

## Setup
```
conda env create -f environment.yml && conda activate prism-data-wrangler   # preferred (all conda-forge)
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```
Keep `environment.yml` and `requirements.txt` in sync when adding dependencies.
To run non-interactively without activating, use `conda run -n prism-data-wrangler python ...`.
The "numpy.ndarray size changed" RuntimeWarning in tests comes from netCDF4 and is harmless.

## Run
```
python scripts/find_huc.py animas [--digits 4 8] [--state CO]
python scripts/prism_watershed.py --huc 14080104 --start 2026-10-20 --end 2026-10-27 [--out DIR]
python scripts/prism_watershed.py --boundary basin.gpkg --start ... --end ...
python scripts/prism_watershed.py --huc ... --start ... --end ... --out DIR --refresh [--target-release 8] [--refresh-age 7]
python scripts/storm_total.py --out DIR [--start ... --end ...] [--tif depth_mm.tif] [--volume-tif m3.tif] [--volume-nc m3.nc]
python scripts/validate_stations.py --out DIR [--start ... --end ...] [--buffer-km 10]
python scripts/validate_runoff.py --out DIR --all-gauges [--after-days 7] [--refresh]
python scripts/validate_runoff.py --out DIR --gauge 09342500 09361500
python scripts/validate_snow.py --out DIR [--start ... --end ...] [--all-snow-c 0 --all-rain-c 3]
python scripts/subbasin_totals.py --out DIR [--digits 10|12] [--start ... --end ...]
python scripts/build_qgis_project.py --out DIR [--name NAME] [--qgis-python PATH]
python -m pytest tests -q
```
On this Mac the shell sets `PROJ_DATA=/opt/anaconda3/share/proj`, an old `proj.db` that breaks pip
rasterio ("EPSG code is unknown … comes from another PROJ installation"). Use the activated
`prism-data-wrangler` env (or `conda run -n prism-data-wrangler`), `.venv`, or prefix with `env -u PROJ_DATA`.

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
- Filenames use resolution codes: `30s` = 800m, `25m` = 4km, `15s` = 400m. Native CRS NAD83 (EPSG:4269), units mm.
- A PRISM "day" ends at 12:00 UTC, so pad event windows by a day on each side.

## Watershed boundaries
USGS WBD REST: `https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/<layer>/query`
with layer = HUC digits / 2 (1→HUC2 … 8→HUC16), field `huc2`…`huc16`, plus `name`, `states`, `areasqkm`.
Example HUCs: 1408 San Juan basin; 14080101 Upper San Juan; 14080104 Animas.

## Validation data facts (checked 31 Oct 2026)
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

## Conventions
- Python 3.10+, standard argparse CLIs, no notebooks in `scripts/`.
- Keep network code separate from raster processing so processing stays testable offline.
- Check the PRISM and USGS endpoints before relying on them. Endpoint changes break this project more than anything else.
