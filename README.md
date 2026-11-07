# PRISM Storm Analysis

Download PRISM daily climate grids (800 m / 4 km) from Oregon State University, clip them to any US
watershed, and summarize storm events: daily basin statistics, storm-total rainfall, and the volume
of water that fell on the basin for any date range.

Project page: https://conklingeospatial.com/projects/prism-storm-analysis/

Commands:

| Command | What it does |
|---|---|
| `scripts/pipeline.py` | Runs the whole workflow for a folder from its settings (`init`, `run`, `status`, `update`); resumes, and picks up PRISM and USGS revisions |
| `scripts/find_huc.py` | Looks up USGS watershed (HUC) codes by name |
| `scripts/prism_watershed.py` | Downloads daily PRISM grids, clips them to a watershed, writes daily rasters, a summary CSV, an event-total raster, and a daily NetCDF cube |
| `scripts/storm_total.py` | Reads the cube and reports depth and water volume for any date range, with optional depth and volume rasters / NetCDF |
| `scripts/storm_context.py` | Compares the storm with PRISM's 1991–2020 normals (percent of normal, with an optional map) |
| `scripts/subbasin_totals.py` | Storm depth and water volume for the whole basin and each subbasin (HUC10/HUC12), as map layers |
| `scripts/validate_stations.py` | Compares PRISM with rain gauges (COOP, CoCoRaHS, airports, and SNOTEL) in and around the basin; writes a styled GeoPackage for QGIS |
| `scripts/validate_runoff.py` | Compares PRISM precipitation volume with streamflow at every USGS gauge in the basin (runoff ratio), flagging gauges regulated by dams |
| `scripts/validate_snow.py` | Checks the rain/snow split against snow-water gained at SNOTEL stations |
| `scripts/build_qgis_project.py` | Builds an organized, styled QGIS project from a folder's outputs (run last) |
| `scripts/report.py` | Writes a self-contained HTML storm report with charts, plus each chart as SVG (run last) |
| `scripts/check_endpoints.py` | Checks that every external service the tools use is answering |

## Setup

```bash
git clone https://github.com/stephenconklin/PRISM_Storm_Analysis.git
cd PRISM_Storm_Analysis
```

### Conda (recommended)
`environment.yml` installs everything from conda-forge, so GDAL, PROJ, rasterio, and netCDF4 are built
to work together.
```bash
conda env create -f environment.yml
conda activate prism-storm-analysis
```
Run all commands from the project folder with this environment active. Activation matters: it points
`PROJ_DATA` at the environment's own PROJ database, so calling the environment's `python` by full path
without activating can still hit the `CRSError` below. To pick up changes to
`environment.yml` later:
```bash
conda env update -f environment.yml --prune
```
Avoid adding these packages to an existing environment (such as Anaconda `base`) with pip: mixing
pip's rasterio with conda's PROJ is what causes the `CRSError` under Troubleshooting.

### pip / venv
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```
`truststore` (included) is needed to download from PRISM; see Troubleshooting.

## Quick start
The pipeline runs everything, in the right order, from one settings file per folder:
```bash
python scripts/pipeline.py init                  # find the watershed, enter the storm dates, then run
python scripts/pipeline.py status --out data/huc<HUC>    # later: what PRISM has revised, what is pending
python scripts/pipeline.py update --out data/huc<HUC>    # pick up the revisions and see what changed
```
See `pipeline.py` below. The same steps by hand (replace the `<…>` placeholders with your watershed and dates):
```bash
# 1. Find your watershed's HUC code by name
python scripts/find_huc.py "<watershed name>"

# 2. Download and clip the storm, padded by a day on each side (writes to data/huc<HUC>)
python scripts/prism_watershed.py --huc <HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD>

# 3. Total rainfall and water volume for any window inside it
python scripts/storm_total.py --out data/huc<HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD>
```
Optional, in this order (each reads what the earlier steps wrote; use the same dates):
```bash
# daily mean temperature, for the rain/snow split and rain-only runoff ratios
python scripts/prism_watershed.py --huc <HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD> --var tmean
# gauges first, so storm_total.py's uncertainty includes the gauge comparison
python scripts/validate_stations.py --out data/huc<HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD>
python scripts/storm_total.py --out data/huc<HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD>
python scripts/subbasin_totals.py --out data/huc<HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD>
python scripts/storm_context.py --out data/huc<HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD>
python scripts/validate_runoff.py --out data/huc<HUC> --all-gauges --start <YYYY-MM-DD> --end <YYYY-MM-DD>
python scripts/validate_snow.py --out data/huc<HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD>
python scripts/build_qgis_project.py --out data/huc<HUC>
python scripts/report.py --out data/huc<HUC>
```

## `pipeline.py`: the whole workflow in one command

```bash
python scripts/pipeline.py init                 # asks for the watershed (name search) and the storm dates
python scripts/pipeline.py run --out data/huc<HUC>
python scripts/pipeline.py status --out data/huc<HUC>
python scripts/pipeline.py update --out data/huc<HUC>
python scripts/pipeline.py migrate --out <folder> --dry-run   # a folder made before 7 Nov 2026 (see Folder layout)
```

`init` writes the folder's settings, `<out>/storm.toml`, and offers to start the run. Everything can
also be given as options, for scripts and notebooks:
```bash
python scripts/pipeline.py init --find animas --state CO --start 2026-10-21 --end 2026-10-27 --run
python scripts/pipeline.py init --huc 1408 --start 2026-10-21 --end 2026-10-27 --event trudy --out data/huc1408
```
With `--find` it searches WBD names as `find_huc.py` does and lists the matches to choose from. The
download window is the storm padded by `--pad` days (default 1) on each side.

### Settings: `storm.toml`

```toml
name = "San Juan, late October 2026"
huc = "1408"                 # or boundary = "basin.gpkg" (relative to this file)
res = "800m"
prism_store = "../prism_store" # optional: national PRISM data shared by every basin (the default; see Notes)

[download]                   # PRISM days to download
start = 2026-10-20
end = 2026-11-02
vars = ["ppt", "tmean"]

[[events]]                   # analysis windows; scripts default to the first
name = "trudy"
start = 2026-10-21
end = 2026-10-27

[[events]]
name = "second"
start = 2026-10-30
end = 2026-11-01

[rain_snow]
all_snow_c = 0.0
all_rain_c = 3.0

[validation]
buffer_km = 10.0
after_days = 7
gauges = []                  # empty = every USGS gauge in the basin

[subbasins]
digits = 10

[report]
gauges = []                  # gauges for the storm timeline; empty = picked automatically
```

Every script reads it for its defaults, and options on the command line override it, so once a folder
has settings most commands need only `--out` (and `--event <name>` for an event other than the first):
```bash
python scripts/storm_total.py --out data/huc1408 --event second
python scripts/prism_watershed.py --out data/huc1408 --var tmean --refresh
```
Each script prints the settings it took from the file. Keeping the rain/snow ramp and window in one
place means `storm_total.py`, `validate_runoff.py`, `validate_snow.py` and the report always agree.

### Several storms in one basin

Keep one folder per basin and add each storm as an event; the folder's daily cube holds every day, and
each event gets its own totals, uncertainty, validation results (files and map layers named
`<name>_<start>_<end>`), report, and group and map themes in the QGIS project.

1. In `storm.toml`, extend `[download] end` to a day past the new storm, and add an event:
   ```toml
   [[events]]
   name = "nov1"
   start = 2026-11-01
   end = 2026-11-03
   ```
   An event can also span several storms (for example both, with the dry days between): its runoff ratio
   then doesn't have to split overlapping recessions.
2. If the storms are close together, end the earlier storm's flow window before the next storm's runoff:
   give that event its own `after_days` (it overrides `[validation] after_days`):
   ```toml
   [[events]]
   name = "trudy"
   start = 2026-10-20
   end = 2026-10-27
   after_days = 3
   ```
3. `pipeline.py run --out DIR --event nov1` downloads only the new days and runs that storm; the other
   storms' results are left as they are (rerun one with `--event trudy`).
4. `pipeline.py update --out DIR` later brings every storm up to date.

An event's own settings follow its window: a script given `--start`/`--end` that match an event (as the
pipeline does) uses that event's `after_days`.

### `run`

Runs the steps in order, once per event where it applies:

| Step | Script | Notes |
|---|---|---|
| `ppt` | `prism_watershed.py` | Download window; a failure stops the run |
| `tmean` | `prism_watershed.py --var tmean` | When `tmean` is in `[download] vars` |
| `stations` | `validate_stations.py` | Before `totals`, so the uncertainty includes the gauge comparison |
| `totals` | `storm_total.py` | Writes `ppt_<res>_total_`, `_volume_`, `_rain_`, `_snow_<start>_<end>.tif`; a failure stops the run |
| `subbasins` | `subbasin_totals.py` | |
| `normals` | `storm_context.py --pct-tif ppt_<res>_pct_normal_<start>_<end>.tif` | |
| `runoff` | `validate_runoff.py` | Rate-limited runs resume where they stopped |
| `snow` | `validate_snow.py` | |
| `report` | `report.py` | |
| `qgis` | `build_qgis_project.py` | When QGIS is installed |

The run ends with a summary (illustrative values):
```
Step       Event          Result                                 Time  Log
ppt        —              ok                                     41 s  run/logs/20261105-120004/01-ppt.log
tmean      —              done earlier
stations   trudy          ok                                      9 s  run/logs/20261105-120004/03-stations-trudy.log
totals     trudy          ok                                     12 s  run/logs/20261105-120004/04-totals-trudy.log
...
runoff     trudy          incomplete                            6.2 min  run/logs/20261105-120004/07-runoff-trudy.log
```

- **Resume:** `run/pipeline_state.json` records each step's result and a fingerprint of its inputs (its
  options, `storm.toml`, the PRISM releases in the folder, and for `totals` the gauge comparison). A
  rerun skips steps that finished with the same inputs, so after a failure, an interruption or the USGS
  rate limit, run the same command again. The report and QGIS project are rebuilt whenever an earlier
  step ran.
- **Failures:** a failed download (`ppt`) or total stops the run, since later steps need them
  (`--keep-going` continues anyway). A failed network step (a service down, a rate limit) is recorded and
  the run goes on; the summary lists it and the run exits with status 3.
- **Choosing steps:** `--from runoff` reruns from that step on; `--only report qgis`; `--skip snow`;
  `--event trudy`; `--force` reruns everything; `--no-qgis`.
- **`--dry-run`:** prints each step's command and whether it would run, checks that the services the
  planned steps use are answering (`check_endpoints.py`; `--offline` skips this), and compares the space
  the remaining downloads need with the free disk space.
- **`--open`:** opens the reports and the QGIS project when the run ends.

### `status` and `update`

`status` shows, without downloading anything, what the folder holds and what to do next (illustrative values):
```
San Juan, late October 2026: HUC 1408, 800m; settings data/huc1408/storm.toml
  ppt    14/14 days (r2: 6, r3: 8); newer at PRISM for 6: 10-27 r2->r3, 10-28 r2->r3, ...
  tmean  14/14 days (r2: 6, r3: 8); newer at PRISM for 6: ...
  PRISM revises days for about 6 months; final releases (r8) expected by about 2027-05-04.
  event trudy (2026-10-21 to 2026-10-27): stations ✓  totals ✓  subbasins ✓  normals ✓  runoff ✓  snow ✓  report ✓
    gauges: 0 with the flow window not over when fetched, 22 with provisional USGS flow
  QGIS project: qgis/huc1408.qgz
Next:
  - PRISM has revised 6 ppt day(s): pipeline.py update --out data/huc1408
```
It asks PRISM's release service which release each day is at (`--offline` skips that), finds outputs made
from older releases (each manifest entry records the releases it used), and gauges whose flow window
hadn't ended when they were fetched.

`update` shows the same status, asks before going on, then:
1. refreshes `ppt` and `tmean` (`--refresh`: only days PRISM has revised are downloaded again),
2. reruns every event's steps (runoff re-fetches gauges whose flow window was cut short, and gauges whose
   flow was provisional when fetched 30 or more days ago, to pick up USGS approvals),
3. keeps the previous report in the event's `report/history/` and rebuilds the QGIS project,
4. prints what changed per event: volume and range, releases, PRISM vs gauges, runoff ratios, snow check.

`update --check` only prints the status; `--notify` adds a desktop notification (macOS) when there is
something to do. PRISM publishes a revision at about D+1, D+5 and then monthly, so a weekly check is
plenty. A launchd job (macOS) for that, in `~/Library/LaunchAgents/com.prism-storm.update.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.prism-storm.update</string>
  <key>ProgramArguments</key><array>
    <string>/opt/anaconda3/bin/conda</string><string>run</string><string>-n</string><string>prism-storm-analysis</string>
    <string>python</string><string>/path/to/PRISM_Storm_Analysis/scripts/pipeline.py</string>
    <string>update</string><string>--out</string><string>/path/to/data/huc1408</string>
    <string>--check</string><string>--notify</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>8</integer></dict>
</dict></plist>
```
Load it with `launchctl load ~/Library/LaunchAgents/com.prism-storm.update.plist`. It runs on this
machine (the data folder and PRISM's per-IP download limit are here) and only checks; run `update`
yourself when it says there is something to do.

### Logs, exit codes and interrupted runs

- **Logs:** every script copies its output, with a time stamp per line, to `<out>/run/logs/` (pipeline steps
  to `run/logs/<time>/<nn>-<step>.log`), together with debug detail the terminal doesn't show: each HTTP
  request with its status, size and time, retries, and rate-limit waits. `--verbose` shows that detail
  in the terminal too; `--quiet` shows only warnings and errors (the log still has everything). The API
  key is sent in a header and never logged. Each `run/run_manifest.json` entry names its log.
- **Exit codes** (every script):

  | Code | Meaning |
  |---|---|
  | 0 | OK |
  | 1 | QC failed (outputs were written) |
  | 2 | Bad options or missing inputs |
  | 3 | Incomplete: rate-limited or partly done; rerun to continue |
  | 4 | Network failure (run `check_endpoints.py`) |
  | 5 | Other error |
  | 130 | Interrupted (Ctrl-C) or cancelled |
- **Every run is recorded**, including ones that fail, are interrupted or are cancelled: the manifest
  entry's `status` is one of `ok`, `qc_fail`, `input_error`, `incomplete`, `network_error`, `error`,
  `interrupted`, with the error message.
- **Ctrl-C** prints what was kept and the command that continues. Downloads, clipped days, CSVs, JSON,
  the cube, rasters and the manifest are written to a `.part` file and renamed when complete, and zips
  are unzipped into a temporary folder, so an interrupted run never leaves a half-written file under its
  real name. Rerunning the same command picks up where it stopped.
- **One run per folder at a time:** a run holds `<out>/.lock` (pipeline steps share the pipeline's). A
  second run in the same folder stops with a message naming the first; a lock left by a run that is
  gone is taken over automatically.
- **Network retries:** a dropped connection or timeout is retried (with backoff for the validation
  services; once for a PRISM download, since each request may count toward the 2-per-24-hours limit;
  never after PRISM's own reply such as its limit message). USGS rate-limit waits show a countdown.
- **Questions:** at a terminal, `--refresh` asks before re-downloading revised PRISM files (they count
  toward the download limit), and `validate_runoff.py --refresh` asks before re-fetching every gauge.
  `--yes` answers yes; without a terminal (pipelines, scheduled jobs) nothing is asked.

### `check_endpoints.py`: are the services up?

```bash
python scripts/check_endpoints.py
python scripts/check_endpoints.py --only prism_releases usgs
```
```
  ok    prism_releases    1.7 s  release 2 for 2026-10-26
  ok    prism_grids       0.2 s  grid count 2
  ok    prism_normals     0.9 s  3.2 MB zip
  ok    wbd              13.7 s  HUC4 query answered
  ok    usgs              0.3 s  SAN JUAN RIVER AT PAGOSA SPRINGS, CO
  ok    nldi              0.8 s  gauge feature returned
  ok    nid               0.3 s  5 dams in the probe box
  ok    acis              1.9 s  10 stations in the probe box
  ok    awdb              1.4 s  118 Colorado SNOTEL stations
```
One light request per service (PRISM release and grid-count services, a HEAD request for the normals,
count queries, one USGS request); nothing counts toward a download limit. Exits 4 if any fails. Changes
to these services break this project more than anything else, so run it when a step fails with a
network error. `pipeline.py run --dry-run` runs it for the services the planned steps use.

## Choosing a watershed
Any watershed in the contiguous US works.

- **`--huc <code>`:** any USGS Watershed Boundary Dataset (WBD) hydrologic unit code, 2 to 16 digits.
  The number of digits picks the level: 2 region, 4 subregion, 6 basin, 8 subbasin, 10 watershed,
  and 12 subwatershed (14 and 16 where mapped). The boundary is fetched from the USGS WBD service and cached.
- **`--boundary <file>`:** any polygon file GeoPandas can read (GeoPackage, shapefile, GeoJSON…) with a
  defined CRS. Multiple polygons are dissolved into one.

### `find_huc.py`: look up HUC codes by name
```bash
python scripts/find_huc.py animas                       # HUC4 through HUC10
python scripts/find_huc.py "san juan" --digits 4 8      # only HUC4 and HUC8
python scripts/find_huc.py "rio grande" --state NM      # only units touching New Mexico
```
Case-insensitive partial match on the WBD name. Prints each match's code, level, area, and states:
```
HUC              Level           Area km²  States       Name
14080104         subbasin           3,547  CO,NM        Animas
15040003         subbasin           5,864  AZ,NM        Animas Valley
1408010401       watershed            413  CO           Headwaters Animas River
```

| Flag | Default | Notes |
|---|---|---|
| `name` | — | Part of the watershed name |
| `--digits` | `4 6 8 10` | HUC levels to search: any of 2, 4, 6, 8, 10, 12, 14, or 16 |
| `--state` | — | Two-letter state code the unit must touch |
| `--limit` | `50` | Max results per level |

Other ways to find codes:
- [USGS Watershed Boundary Dataset](https://www.usgs.gov/national-hydrography/watershed-boundary-dataset): what HUCs are and how they nest
- [The National Map viewer](https://apps.nationalmap.gov/viewer/): turn on the Watershed Boundary Dataset layer and click a watershed to see its code
- [WBD map service](https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer): the service both scripts query

PRISM covers the contiguous US only, so parts of a basin in Canada or Mexico, or in Alaska, Hawaii, or
Puerto Rico, get no data. `prism_watershed.py` prints a warning with the share of the basin that has no
PRISM data, and `storm_total.py` repeats it: depth and volume then cover only the US part. Gauges whose
basins reach more than `--max-missing-pct` outside PRISM coverage are skipped by `validate_runoff.py`.

## `prism_watershed.py`: download, clip, summarize

```bash
python scripts/prism_watershed.py --huc 14080104 --start 2026-10-20 --end 2026-10-27
python scripts/prism_watershed.py --huc 1408 --start 2026-10-20 --end 2026-10-27 --out /path/to/My_Basin
python scripts/prism_watershed.py --boundary my_basin.gpkg --start 2026-10-20 --end 2026-10-27
python scripts/prism_watershed.py --huc 1408 --start 2026-10-20 --end 2026-10-27 --out /path/to/My_Basin --refresh
```

| Flag | Default | Notes |
|---|---|---|
| `--start` / `--end` | required (or from `storm.toml`) | PRISM days, inclusive; end is capped at yesterday |
| `--huc` / `--boundary` | one required | Watershed to clip to (see Choosing a watershed) |
| `--out` | `data/huc<code>` or `data/<boundary name>` | Output folder, relative to where you run the command. Use one folder per basin |
| `--var` | `ppt` | `tmin`, `tmax`, `tmean`, `tdmean`, `vpdmin`, or `vpdmax`. Other variables can share a precipitation folder: they get their own cube and `<var>_<res>_daily_basin_summary.csv` (basin mean, min, and max in °C or hPa) and no event total. `tmean` enables the rain/snow split |
| `--res` | `800m` | or `4km` |
| `--region` | `us` | PRISM region |
| `--format` | COG GeoTIFF | `nc`, `bil`, or `asc` |
| `--pause` | `2.0` | Seconds between download requests |
| `--store` | `prism_store` in storm.toml, else `PRISM_STORE`, else `prism_store/` next to `--out` | The PRISM store: national zips and unzipped grids shared by every basin (see Notes; `--cache` is the older name) |
| `-y` / `--yes`, `-v` / `--verbose`, `-q` / `--quiet` | off | Don't ask before `--refresh` re-downloads; show debug detail; show only warnings (every script takes these) |
| `--refresh` | off | Re-download cached days that PRISM has a newer release of, up to `--target-release` (see Updating to newer PRISM releases) |
| `--target-release` | `8` | Release to refresh up to (8 is final) |
| `--refresh-age` | `7` | Only when PRISM's release service can't be reached: re-download zips last downloaded at least this many days ago |
| `--qc-strict` | off | Treat QC warnings as failures (see QC checks and run manifest) |
| `--qc-max-daily-mm` | `400` | Flag a daily cell above this much precipitation |
| `--qc-station-buffer-km` | `10` | Count PRISM input stations this far outside the basin |
| `--qc-min-stations` | `0.5` | Warn below this many PRISM input stations per 1,000 km² |

### Folder layout

Every tool writes into the same layout (`scripts/layout.py`). One folder per basin:

```
<out>/
  storm.toml          the folder's settings (pipeline.py init); every script reads its defaults from it
  basin/              wbd_huc<code>.geojson (from WBD, with --huc) or boundary.geojson (with --boundary);
                      gauges/<site>_basin.geojson (USGS gauge basins); dams.gpkg (NID dams)
  daily/              every PRISM day in the folder, whatever the storm
  events/<event>/     one folder per storm window (an event in storm.toml; other windows: <start>_<end>/)
    event.json        the window and event name
    totals/           depth, volume, rain, snow, percent-of-normal rasters; uncertainty and normals JSON
    subbasins/        totals.gpkg, subbasin_totals_<start>_<end>.csv
    validation/       station, SNOTEL and runoff results, validation.gpkg; runoff/ per gauge
    report/           storm_report_<start>_<end>.html, figures/, history/, web/
  qgis/               the QGIS project
  run/                pipeline_state.json, run_manifest.json, logs/, .lock
prism_store/          next to the basin folders: national PRISM data shared by every basin (see Notes)
```

Files inside an event folder keep their window in their name (`_<start>_<end>`), so a file copied out of
the folder still says which storm it covers. A folder made before 7 Nov 2026 (everything at the top
level) is moved to this layout with `pipeline.py migrate --out <folder>` (offline; `--dry-run` lists the
moves); the tools say so when they meet one.

`prism_watershed.py` writes:

| File | Contents |
|---|---|
| `basin/wbd_huc<code>.geojson` | Cached basin boundary from USGS WBD (with `--huc`) |
| `basin/boundary.geojson` | With `--boundary`: the basin polygon, kept so later steps and the QGIS project find it |
| `daily/clipped/<var>_<res>_YYYYMMDD_clip.tif` | Each day clipped to the basin (mm for `ppt`), tagged with its PRISM release (`PRISM_DATASET_RELEASE_NUMBER`) |
| `daily/daily_basin_summary.csv` | Per day: PRISM release number, area-weighted basin mean and max (mm), and basin mean (in). Covers every day in the folder; a run replaces its own days. Named `<var>_<res>_daily_basin_summary.csv` for other variables or 4 km |
| `daily/<var>_<res>_release_history.csv` | One row per day and PRISM release seen in this folder: basin mean and max (mm) and volume (m³) for `ppt`; mean, min, and max for other variables; and when recorded |
| `daily/<var>_<res>_total_<start>_<end>.tif` | Sum of the downloaded days per pixel (mm for `ppt`) |
| `daily/<var>_<res>_daily_clip.nc` | NetCDF cube: every clipped day in the folder on a time axis (`<var>`, mm) plus `cell_area` (m²) and `release` (PRISM release per day; 0 unknown). Rebuilt on each run, so new days are added automatically |
| `run/run_manifest.json` | One entry per run of any script: command, status, log, versions, inputs (and the PRISM releases a windowed output was made from), outputs with checksums, and QC results |
| `run/logs/` | Each run's output with time stamps and HTTP detail; `run/logs/<time>/` for pipeline runs |
| `run/pipeline_state.json` | What each pipeline step last did and with which inputs, for resuming |
| `run/.lock` | Present while a run is writing to the folder |

The national zips and their unzipped grids go to the PRISM store: `zips/<region>/<res>/<var>/` (superseded
releases in `releases/`, as `<name>_r<release>.zip`), `extracted/<region>/<res>/<var>/<name>_r<release>/`
(including each day's `info.txt`), and `normals/` for `storm_context.py`.

All rasters are on PRISM's native grid (1/120°, ≈800 m) in NAD83 (EPSG:4269), nodata −9999 or NaN
outside the basin.

### QC checks and run manifest

Each run of `prism_watershed.py` and `storm_total.py` ends with a QC block, for example (San Juan,
PRISM days Oct 20–27):

```
QC: WARN
  PASS  completeness         all 8 days present
  PASS  download_integrity   8 zips open; SHA-256 recorded
  PASS  grid_consistency     8 days on one grid (292 x 518 cells)
  PASS  nodata_inside        no nodata inside the basin
  PASS  physical_limits      no negatives; largest daily cell 101.7 mm (limit 400)
  PASS  totals_reconcile     daily clip volumes = cube volumes agree
  WARN  release_mix          8 days at release 1-2 (PRISM revises them within weeks)
  PASS  station_density      137 stations in the basin or within 10 km of it on 2026-10-21 (2.12 per 1,000 km²; warn below 0.5)
  PASS  boundary_agreement   grid 64,558.0 km² vs polygon 64,569.1 km² (-0.02%; warn beyond 1%)
```

| Check | Fails or warns when |
|---|---|
| `completeness` | A requested day was not downloaded or is not in the cube (fail) |
| `download_integrity` | A download returned no zip (PRISM's message is quoted), a request failed, or a zip is corrupt (fail). A corrupt cached zip is renamed `.bad` so the next run downloads it again. A failed `--refresh` is recorded in the manifest (`refresh_error`) but doesn't fail the check, since the cached zip is used |
| `grid_consistency` | Days differ in transform, shape, or CRS (fail) |
| `nodata_inside` | Basin cells lack data on some days only: above 0 (warn) or 1% (fail). A gap that is the same every day, such as the part of a basin in Canada or beyond PRISM's grid, warns above 0.5% of the basin |
| `physical_limits` | Negative precipitation (fail) or a daily cell above `--qc-max-daily-mm` (warn). `ppt` only |
| `totals_reconcile` | The volumes computed from each day's clip differ from the cube's, read back; in `storm_total.py`, the volume raster, the national-grid volume by cell center, or rain + snow differ from the reported volume (fail) |
| `release_mix` | The window has release 1–2 days, unknown releases, or a mix of releases (warn) |
| `station_density` | Fewer than `--qc-min-stations` PRISM input stations (from each day's `stn.csv`) per 1,000 km² in the basin plus `--qc-station-buffer-km` (warn) |
| `boundary_agreement` | The area of the cells counted as the basin differs from the polygon's ellipsoidal area by more than 1% (warn) |

`storm_total.py` runs the checks that apply to a window: `nodata_inside`, `physical_limits`,
`totals_reconcile`, `release_mix`, and `boundary_agreement` (the cube is one grid, missing days are
already an error, and station density comes from the zips). `--qc-strict` turns warnings into
failures. If any check fails, the script still writes its outputs, then exits with status 1.

`run_manifest.json` keeps one entry per run, so a folder refreshed over months keeps its whole history.
Each entry has the command line and arguments, start and end time, git commit (with `-dirty` for
uncommitted changes), Python and package versions (including GDAL and PROJ), the basin source (HUC and
WBD query, or boundary file and SHA-256), per day the PRISM URL, zip SHA-256, release, and all
`info.txt` fields, the output files with SHA-256, and the QC results. `storm_total.py` entries also
record the window results.

### Updating to newer PRISM releases

PRISM revises each daily grid up to 8 times over about 6 months: release 1 the day after, release 2
about 4 days later, and then about monthly; release 8 is final. To bring a folder up to date, rerun the
same command with `--refresh`:

```bash
python scripts/prism_watershed.py --huc 1408 --start 2026-10-20 --end 2026-10-27 --out /path/to/My_Basin --refresh
```

- Every run asks PRISM's release service which release it has for each day (one small request, no
  grids downloaded). With `--refresh`, only cached days with a newer release (up to `--target-release`)
  are downloaded again; without it, the run tells you how many days have a newer release. Each
  re-download counts toward PRISM's limit of two downloads of a file per 24 hours.
- If the release service can't be reached, `--refresh` falls back to re-downloading days whose zip is at
  least `--refresh-age` days old; a re-download may then bring back the same release, and the zip's
  date records when it was last checked.
- A newer release **replaces** the day in `daily/clipped/`, the cube, and the summary CSV, so all later steps
  use the newest data. Event-total rasters from other windows that include a revised day are listed as
  out of date, with the command to update them. The superseded zip moves to the store's `releases/`, and the old and new basin
  mean and volume stay in `<var>_<res>_release_history.csv`. The run prints each revised day as
  `<date>: release <old> -> <new>: <old> -> <new> mm, <old> -> <new> m³ (<change>%)`.
- `storm_total.py` lists each day's release in its report, notes days that are not final, and reports
  how much the window volume has changed since the first releases recorded.
- First real refresh (San Juan, 1 Nov 2026): Oct 26–27 went from release 1 to 2, Oct 26's basin volume
  fell 18.9%, and the Oct 20–27 window volume fell 0.7% (4.719 to 4.685 km³).
- `tmean` has its own releases: refresh it too (`--var tmean --refresh`) if you use the rain/snow split.
- `pipeline.py update` does all of this (both variables, every event, the report and QGIS project) and
  prints what changed. By hand, rerun the later steps in this order, for the same window: `validate_stations.py`, then
  `storm_total.py` (its uncertainty reads the station comparison), then `subbasin_totals.py`,
  `validate_runoff.py`, and `validate_snow.py`; last, `build_qgis_project.py` (its basin label reads
  the uncertainty). `validate_runoff.py` notices the new releases and recomputes each saved gauge's
  precipitation and runoff ratio from the saved basin and flow, with no new requests per gauge.

## `storm_total.py`: depth and water volume for any date range

```bash
python scripts/storm_total.py --out data/huc14080104                                  # whole cube
python scripts/storm_total.py --out data/huc14080104 --start 2026-10-22 --end 2026-10-23
python scripts/storm_total.py --out data/huc14080104 --start 2026-10-20 --end 2026-10-27 \
    --tif data/huc14080104/depth_mm.tif --volume-tif data/huc14080104/volume_m3.tif --volume-nc volume_m3.nc
```
Output paths are relative to where you run the command. Put rasters in the `--out` folder, named for what
they hold (`depth`, `volume`/`m3`, `rain`, or `snow`), so `build_qgis_project.py` finds and styles them.

| Flag | Default | Notes |
|---|---|---|
| `--out` | required | Folder `prism_watershed.py` wrote to; reads `ppt_<res>_daily_clip.nc` |
| `--res` | `800m` | or `4km` |
| `--start` / `--end` | first / last day in the cube | PRISM days, inclusive. Days missing from the cube are an error |
| `--tif` | — | Write total depth per pixel (mm) to this GeoTIFF |
| `--volume-tif` | — | Write water volume per pixel (m³ = depth × cell area) to this GeoTIFF; its sum is the basin volume |
| `--volume-nc` | — | Write a NetCDF with `volume` (m³ per pixel per day), `volume_total` (window total), and `cell_area` |
| `--boundary` | the `wbd_huc*.geojson` (or `boundary.geojson`) in `--out` | Basin polygon for the boundary check |
| `--ramp-alternatives` | `-1,3 1,1` | Other rain/snow ramps (°C pairs) for the rain volume range |
| `--all-snow-c` / `--all-rain-c` | `0` / `3` | Rain/snow ramp (see Rain and snow) |
| `--rain-tif` / `--snow-tif` / `--rain-snow-nc` | — | Rain/snow outputs (see Rain and snow) |
| `--qc-strict` | off | Treat QC warnings as failures |
| `--qc-max-daily-mm` | `400` | Flag a daily cell above this much precipitation |

It prints the basin area, area-weighted mean depth, max depth, and the total water volume in m³, km³,
and acre-feet, then each day's PRISM release, the revision change, the rain/snow split (with a `tmean`
cube), the boundary check, the uncertainty, and a QC block. San Juan, PRISM days Oct 20–27, after the
Oct 26–27 refresh to release 2:

```
PRISM days 2026-10-20 to 2026-10-27 (8 days; each ends 12:00 UTC on its date)
Basin area:  64,558 km²
Mean depth:  72.6 mm (2.86 in)
Max depth:   228.5 mm (9.00 in)
Volume:      4.685e+09 m³ = 4.685 km³ = 3,797,849 acre-ft
Releases:    r2 Oct 20-Oct 27
             Release 8 is final; earlier releases change for about 6 months. To pick up revisions, re-run prism_watershed.py with --refresh.
Revisions:   window volume -0.7% since the first releases recorded (4.719e+09 -> 4.685e+09 m³); revised: Oct 26 r1->r2, Oct 27 r1->r2
Rain/snow:   rain 70.0 mm, 4.521 km³ (96.5%); snow 2.5 mm, 0.164 km³ (3.5%) [all snow at or below 0 °C, all rain at or above 3 °C, daily mean temperature]
Boundary:    cell centers 4.6846 km³, area-weighted 4.6852, any cell touched 4.7634 (range 1.68% of area-weighted)
Uncertainty: 4.685 km³, range 4.625-4.916 km³ (-1.3% to +4.9%; components added, see docs/UNCERTAINTY.md)
  gauges     -0.5% to +4.2%   all gauges (n=88) PRISM -4.0% -> volume +4.2%; independent gauges (n=7) PRISM +0.5% -> volume -0.5%. ...
  boundary   +0.0% to +0.0%   area-weighted +0.01% (any cell touched +1.68%, not used: it counts land outside the basin)
  revisions  -0.7% to +0.7%   window volume changed -0.72% from first recorded to latest releases; 8 of 8 days not final (release 2), ...
  products   not assessed     comparison with other precipitation products (roadmap 1.3) not built
  rain       4.521-4.573 km³ across ramps 0,3 (used) and -1,3, 1,1 °C; shifts water between rain and snow, not the total
```

- **Uncertainty:** the volume with a bounding range and its components: gauge comparison
  (`validate_stations.py` for the same window), basin boundary, PRISM revisions so far, and other
  products (not yet assessed); with a `tmean` cube, also the rain volume across alternative ramps
  (`--ramp-alternatives`). Saved to `ppt_<res>_uncertainty_<start>_<end>.json`; the QGIS project
  shows the range on the basin label. It is a bound on what has been measured, not a confidence
  interval; see [docs/UNCERTAINTY.md](docs/UNCERTAINTY.md). San Juan, Oct 20–27: 4.685 km³, range
  4.625–4.916 km³ (−1.3% to +4.9%).
- **Revisions:** from `ppt_<res>_release_history.csv`, the change in window volume from each day's
  first recorded release to its latest, and which days were revised (for example
  `window volume -0.7% since the first releases recorded (...); revised: Oct 26 r1->r2, Oct 27 r1->r2`). This is how
  much PRISM's revisions have moved the total so far, one part of its uncertainty.
- **Boundary:** the window volume counted three ways from the national grids in the PRISM store: cells
  whose centers fall in the basin (the rule the cube and every other total use), every cell weighted by
  its share inside the basin, and every cell the basin touches. The range shows how much the basin edge
  matters; it is small for large basins (1.7% for the San Juan, mostly from the "touched" rule) and
  grows for small ones. Skipped, with a note, when the boundary or a day's national grid is missing.

### Rain and snow

PRISM precipitation includes snow. To split it, download daily mean temperature for the same basin,
days, and folder, then rerun `storm_total.py`:

```bash
python scripts/prism_watershed.py --huc 1408 --start 2026-10-20 --end 2026-10-27 --out /path/to/My_Basin --var tmean
python scripts/storm_total.py --out /path/to/My_Basin --start 2026-10-20 --end 2026-10-27 \
    --rain-tif /path/to/My_Basin/rain_mm.tif --snow-tif /path/to/My_Basin/snow_mm.tif --rain-snow-nc rain_snow.nc
```
```
Rain/snow:   rain 70.0 mm, 4.521 km³ (96.5%); snow 2.5 mm, 0.164 km³ (3.5%) [all snow at or below 0 °C, all rain at or above 3 °C, daily mean temperature]
```

| Flag | Default | Notes |
|---|---|---|
| `--all-snow-c` | `0` | Daily mean temperature (°C) at or below which a cell's precipitation is all snow |
| `--all-rain-c` | `3` | At or above this it is all rain; linear between. Set both equal for a single threshold |
| `--rain-tif` / `--snow-tif` | — | Window-total rain or snow-water per pixel (mm) |
| `--rain-snow-nc` | — | NetCDF with daily `rain`, `snow`, and `snow_fraction`, plus window totals `rain_total` and `snow_total` (mm) |

- The split is made per cell and day: snow = precipitation × snow fraction, rain = the rest, so rain +
  snow equals precipitation (checked by QC). Snow is snow-water equivalent, not snow depth.
- PRISM `tmean` is the average of the day's maximum and minimum and uses the same 12:00 UTC day as
  precipitation. A daily mean is a rough guide to the temperature when precipitation fell: a warm
  afternoon shower on a day with a cold night can be classed as snow, and the reverse. Try other
  thresholds (for example `--all-snow-c -1 --all-rain-c 3`, or `1` and `1`) to see how sensitive the
  split is. For the San Juan, Oct 20–27, snow was 3.5% of the basin's precipitation by default, 2.4% with
  a single 1 °C threshold, and 2.8% with a −1 to 3 °C ramp.
- Without a `tmean` cube the report says the split was skipped and how to enable it.

Cell areas are computed on the NAD83 (GRS80) ellipsoid for each row of PRISM's native grid, so no
reprojection or resampling is needed and volumes add up exactly.

## `storm_context.py`: percent of normal

```bash
python scripts/storm_context.py --out data/huc<HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD>
python scripts/storm_context.py --out data/huc<HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD> \
    --pct-tif data/huc<HUC>/pct_of_normal.tif
```
```
PRISM days 2026-10-20 to 2026-10-27: basin 72.6 mm vs a normal of 7.7 mm for these days (monthly normals prorated by days in the window): 944% of normal
  = 244% of the normal October total (30 mm)
```
Compares the storm with PRISM's 1991–2020 normals over the same basin cells (area-weighted):
- **Normal for these days:** each month's normal times the share of that month's days in the window
  (October × 8/31 above), or with `--daily-normals` the sum of PRISM's daily normals for those dates.
  Daily normals change smoothly within a month, so the two agree closely; daily ones are larger downloads.
- **Month:** the storm as a share of each touched month's whole normal total.
- **Map:** `--pct-tif` writes percent of normal per cell (name it with `normal` or `pct` so the QGIS
  project maps it, with a diverging scale around 100% and a "Percent of normal" theme).

Normals aren't on PRISM's data web service; they come from PRISM's file server
(`data.prism.oregonstate.edu/normals/...`) and are cached in the store's `normals/` (a monthly 800 m normal is
about 58 MB, a daily one about 67 MB; 4 km ones are about 2.5 MB). Writes
`ppt_<res>_normals_<start>_<end>.json`, which `report.py` shows as a headline tile.

## `subbasin_totals.py`: storm volume for the basin and each subbasin
```bash
python scripts/subbasin_totals.py --out data/huc<HUC>                       # subbasin level chosen automatically
python scripts/subbasin_totals.py --out data/huc<HUC> --digits 12 --start <YYYY-MM-DD> --end <YYYY-MM-DD>
```
Puts the storm's water volume on the map. Writes `events/<event>/subbasins/totals.gpkg` with two layers, plus
`subbasin_totals.csv` (subbasins ranked by volume):

| Layer | Contents |
|---|---|
| `basin_total` | The study basin polygon with the storm totals: area, mean and max depth, volume (m³, km³, acre-ft), PRISM window, and coverage |
| `subbasins` | Every WBD unit inside the basin at one HUC level, with area, mean and max depth, volume (m³, acre-ft), share of the basin's volume, rank, and coverage |

- **Subbasin level:** `--digits`, or by default the first level below the study basin with at least
  20 units: HUC10 in a HUC4 (117 in the San Juan), HUC12 in a HUC8 (57 in the Upper Skagit).
  With `--boundary` (no HUC), HUC12 units (unless `--digits`) at least half inside the boundary are used.
- **Same numbers as `storm_total.py`:** cells count when their centers fall in a unit, using the same
  ellipsoidal cell areas, so subbasin volumes add up to the basin total (100.0% for the San Juan).
- **Coverage:** cells without PRISM data (for example in Canada) are left out of depth and volume and
  reported as `coverage_pct`; units entirely outside PRISM coverage have no depth.

Example (San Juan, PRISM days Oct 20–27):
```
Basin: 64,558 km² with PRISM data (100.0% coverage), mean 72.6 mm, max 228.5 mm, 4.685 km³ = 3,797,849 acre-ft
117 HUC10 subbasins; their volumes add up to 100.0% of the basin total
Largest by volume:
    1. Navajo River                                  779 km²   170.4 mm     107,565 acre-ft    2.8%
    2. La Jara Creek                                 749 km²   122.5 mm      74,395 acre-ft    2.0%
    3. Pagosa Springs-San Juan River                 536 km²   157.7 mm      68,480 acre-ft    1.8%
```

## Validation against observations
These commands read the folder `prism_watershed.py` wrote and write to each event's `validation/` folder.
Window dates are PRISM days and default to every day in the cube.

### `validate_stations.py`: rain gauges
```bash
python scripts/validate_stations.py --out data/huc<HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD>
```
- **Stations:** every RCC-ACIS station inside the basin plus `--buffer-km` (default 10): COOP, CoCoRaHS,
  airports (ASOS/first-order), and SNOTEL. PRISM is sampled from the full national grids in the PRISM store,
  so stations just outside the basin are compared too.
- **Day matching:** gauges report 24-hour totals ending at their observation hour (CoCoRaHS ~7 AM,
  SNOTEL and airports midnight). Each is matched to the PRISM day whose 12:00 UTC end is closest;
  days more than `--max-offset` hours apart (default 6) are kept but left out of the statistics.
  Midnight observers are 5 hours from 12:00 UTC in Mountain time but 7 in Eastern time, so in Eastern
  time they drop out of the statistics unless you raise `--max-offset`.
- **In-sample flag:** gauges within `--match-km` (default 0.5) of a station PRISM used that day
  (from the zip's `*.stn.csv`) are marked `in_sample`. PRISM was fitted to those, so their agreement
  is optimistic; `independent` gauges are the fairer test.
- **Storm totals** need every day in the window; accumulated reports (`S` days then an `A` total)
  count only when the accumulation lies inside the window. ACIS can lag a few days for SNOTEL, so
  a window ending within the last week may leave SNOTEL out; end the window a day earlier if so.

Prints bias, MAE, RMSE, correlation, and percent difference (all PRISM minus gauge) for storm totals
and daily values, overall, inside the basin, independent vs in-sample, and by network.

### `validate_runoff.py`: streamflow
```bash
python scripts/validate_runoff.py --out data/huc<HUC> --all-gauges          # every gauge in the basin
python scripts/validate_runoff.py --out data/huc<HUC> --gauge <site> [<site> ...]
```
- **Which gauges:** `--all-gauges` finds every USGS stream gauge inside the study basin (the basin
  `--out` was built for, or `--boundary`) with a 15-minute discharge record covering the window.
  Gauges that can't be compared are kept in the outputs with the reason (see `status` below).
- **Gauge basin:** from USGS NLDI. PRISM precipitation over it is sampled from the national daily grids
  in the PRISM store, so a gauge basin may extend past the study basin (only the gauge must lie inside it;
  NLDI and WBD boundaries often differ, especially in flat terrain). Up to `--max-missing-pct`
  (default 1%) of the gauge basin may lie outside PRISM coverage (Canada or Mexico); those cells get
  the basin-average depth.
- **Flow:** the gauge's 15-minute record from the USGS Water Data API, from the start of the first
  PRISM day to `--after-days` (default 7) after the last, or to the time of the run if sooner.
  Short gaps are interpolated and reported; a gauge with a gap over `--max-gap-hours` (default 12) is
  skipped. Daily values are not used because USGS can withhold a whole day around a short equipment
  problem, often at the flood peak.
- **Regulation:** dams from the National Inventory of Dams anywhere in each gauge's basin are counted.
  A reservoir can hold back no more than the storm's precipitation over its own drainage area, so each
  dam's **effective storage** is the smaller of its capacity (normal storage, else NID storage) and the
  basin's storm depth over the dam's NID drainage area; dams without a drainage area in the NID keep
  their full capacity. Effective storage is spread over the gauge basin as a depth in mm. Under
  `--minor-mm` (default 1 mm) the gauge is **unregulated**, up to `--regulated-mm` (default 10 mm)
  **minor**, above that **regulated**. Capacity is reported too. Example: Electra Lake (Terminal Dam,
  22,000 acre-ft) sits on Elbert Creek with a 26 mi² drainage area, so above Durango it can hold at most
  about 6,800 acre-ft of this storm: 6.2 mm effective of 16.8 mm capacity, **minor**, where counting
  capacity alone called the free-flowing Animas regulated. A regulated gauge's runoff ratio reflects
  reservoir operations as much as precipitation, so read it as a check on the water budget, not on
  PRISM. Not modeled: how full reservoirs were, diversions into a reservoir from outside its drainage
  area (Electra Lake also takes Cascade Creek water), off-channel reservoirs filled by pumping (Lake
  Nighthorse), and transbasin diversions (the San Juan–Chama Project). Dikes listed with their
  reservoir's storage are counted once.
- **Rate limits and resuming:** the USGS Water Data API allows 1,000 requests per hour per IP; a full
  run uses roughly two per gauge plus a few. When USGS asks the tool to pause, it waits (printing how
  long) for up to `--max-wait-min` (default 15). For a longer pause, the run stops, writes what it has,
  and says when to retry. Rerunning the same command reuses
  gauges already finished for the same window with the same settings (each window keeps its own results,
  so running another storm doesn't re-fetch them) (`--refresh` re-fetches everything, for example after
  USGS revises provisional data). If only the PRISM releases or the rain/snow settings changed, saved
  gauges keep their flow and only their precipitation and ratios are recomputed. With a free key from
  [api.waterdata.usgs.gov/signup](https://api.waterdata.usgs.gov/signup/), set `USGS_API_KEY` (for example in
  `~/.zshrc`: `export USGS_API_KEY=...`); it is sent as the `X-Api-Key` header and gets its own
  allowance of 1,000 requests per hour, separate from the anonymous per-IP one. Keep the key out of the
  repository.
- **Runoff ratio** = event runoff ÷ precipitation volume, bracketed by two baseflow lines: held at
  the starting flow (larger runoff) and rising straight to the ending flow (smaller runoff).
  Precipitation includes snow, so ratios in mountain basins are low.
- **Rain-only ratio:** when `tmean` grids for the window are in the PRISM store (`prism_watershed.py
  --var tmean`), each gauge also gets runoff ÷ rain volume and its basin's snow share (`rain_mm`,
  `snow_pct`, `rain_ratio_low`, and `rain_ratio_high` in `runoff_summary.csv`), split as in `storm_total.py`
  (`--all-snow-c`, `--all-rain-c`).
- **Checks printed:** basin area three ways (PRISM cells, NLDI polygon, and USGS published drainage area),
  hours interpolated, and USGS approval status (provisional or approved).

### `validate_snow.py`: snow at SNOTEL stations

Checks the rain/snow split (see Rain and snow) against snow-water equivalent measured at SNOTEL
stations. Needs a `ppt` run over the window in the folder; without a `tmean` run, the PRISM split is skipped.

```bash
python scripts/validate_snow.py --out data/huc<HUC>
python scripts/validate_snow.py --out data/huc<HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD> --all-snow-c -1 --all-rain-c 3
```

| Flag | Default | Notes |
|---|---|---|
| `--start` / `--end` | first / last day in the cube | PRISM days |
| `--buffer-km` | `10` | Also use stations this far outside the basin |
| `--all-snow-c` / `--all-rain-c` | `0` / `3` | The ramp to test, as in `storm_total.py` |
| `--min-precip-mm` | `10` | Leave stations with less precipitation out of the average |

For each active SNOTEL station it reports, as a share of precipitation over the window:
- **observed:** snow-water gained by the snow pillow (peak in the window minus the day before).
  A lower bound on snow that fell, since rain and warm spells melt some within the window.
- **station split:** the station's own precipitation split by its own daily mean temperature with
  the ramp. Tests the method without PRISM.
- **PRISM split:** PRISM precipitation and `tmean` at the station's cell, split with the ramp. This
  is what `storm_total.py` and `validate_runoff.py` use.

Station data come from the NRCS AWDB web service (no key needed). SNOTEL stations are PRISM inputs,
so PRISM precipitation at a station is close to the gauge by construction; the snow share is the
independent test. AWDB days are Pacific Standard Time calendar days, so PRISM day *D* is AWDB day
*D*−1 (checked by lagged correlation, 0.95–0.998).

Example (San Juan, PRISM days Oct 20–27, excerpt):
```
SNOTEL station           elev m  precip SWE gain   snow % of precip: observed  station split  PRISM split
Red Mountain Pass          3371    140mm     69mm                        49             85           54
Wolf Creek Summit          3332    226mm     81mm                        36             36           35
Molas Lake                 3234    132mm     23mm                        17             48           36
Mineral Creek              3057    132mm     30mm                        23             57           46
Cascade #2                 2740    130mm      8mm                         6              9            0

Average over 23 stations with >= 10 mm: observed 18.1%, station split 29.3%, PRISM split 23.1%; PRISM snow / observed SWE gain = 1.16
```
Read as: with the default 0–3 °C ramp, PRISM's snow is 1.16 times the snow the pillows kept, which
fits observed snow being a lower bound. Ramps that end at 2 °C give half the observed snow or less;
−1 to 3 °C gives 0.9 times. Around Silverton (Molas Lake, Mineral Creek) the PRISM split is about
twice the observed share, so the high Animas gauges' rain-only runoff ratios are too high. The
station split runs higher still, likely because a station's daily mean misses warm hours when the
rain fell.

### Outputs (in `events/<event>/validation/`)

Each name ends in the window, `_<start>_<end>`:

| File | Contents |
|---|---|
| `validation.gpkg` | Layers `stations_…`, `station_days_…`, `gauge_basins_…` and `gauges_…`, each with a default QGIS style stored in the GeoPackage, plus `snotel_…` (unstyled) |
| `stations_….csv`, `station_days_….csv` | Same data as the station layers |
| `stations_summary_….json` | All statistics, counts, settings, and exclusion reasons |
| `runoff_summary_….csv` | One row per gauge in the study basin: status, regulation, volumes, and ratios |
| `runoff/runoff_<site>_….json` | Areas, daily basin precipitation, flow volumes, peak, gaps, approval, runoff ratios, and regulation (or the skip reason); reused by later runs for the same window and settings |
| `runoff/runoff_<site>_flow_….csv` | 15-minute flow, interpolated flag, and both baseflow lines |
| `snotel_….csv`, `snotel_summary_….json` | Per SNOTEL station: precipitation, SWE gain, and the three snow shares (or the skip reason); the averages |

and for the whole basin, `basin/gauges/<site>_basin.geojson` (each gauge's NLDI basin, saved so later runs
reuse it) and `basin/dams.gpkg` (layer `dams`). `--all-gauges` replaces the gauge layers; `--gauge` adds or
replaces just those gauges' rows.

`validate_runoff.py --recheck-provisional-days N` re-fetches gauges whose saved flow was provisional
(USGS approves discharge over months) and was fetched N or more days ago; `pipeline.py update` uses 30.

### Reading the validation results

#### Station statistics
Every statistic compares PRISM with the gauge at the same place, as **PRISM minus gauge**: negative
means PRISM is drier than the gauge, positive means wetter. P is the PRISM value at the gauge's cell,
G the gauge value, n the number of pairs.

| Statistic | Formula | Units | Reads as |
|---|---|---|---|
| n | number of PRISM–gauge pairs | — | How many stations (storm totals) or station-days (daily) went into the line. Treat groups with n under ~5 as anecdotes |
| bias | mean(P − G) | mm | Average over- or under-estimate. 0 is ideal; its sign says which way PRISM leans |
| MAE | mean(\|P − G\|) | mm | Typical size of a difference, ignoring sign |
| RMSE | √mean((P − G)²) | mm | Like MAE but weights big misses more; RMSE much larger than MAE means a few large outliers |
| r | Pearson correlation of P and G | — | Whether PRISM gets the pattern right (which places got more), regardless of bias. 1 is perfect |
| percent difference | Σ(P − G) ÷ ΣG × 100 (`pbias_pct` in the JSON) | % | Overall bias relative to the amount of rain; the most useful single number for storm totals |

**Two kinds of comparison:**
- **Storm totals** (`event_totals` in the JSON, the `stations` layer): each gauge's total over the
  whole window against PRISM's total at that cell. This is the main test: totals are what volumes are
  built from, and day-boundary timing differences mostly cancel out.
- **Daily values** (`daily`, the `station_days` layer): each day separately. Expect these to look
  worse than storm totals. A gauge read at 7 AM and a PRISM day ending at 5–8 AM (by time zone) split a storm's rain
  across days differently, so heavy days produce large daily differences that net out over the storm.

**Groups:**
| Group | Meaning |
|---|---|
| all | Every station with a valid storm total (or day) and day alignment within `--max-offset` |
| inside_basin | Only stations inside the basin polygon, excluding the buffer |
| independent | Gauges PRISM did not use; the fairest test, but often few |
| in_sample | Gauges PRISM used; agreement is optimistic because PRISM was fitted to them |
| by network | COOP, CoCoRaHS, ASOS/first-order, SNOTEL, and other |

**Interpreting them:**
- **Good agreement** for a storm total is roughly percent difference within ±10% and r above 0.9. A
  large difference with high r points to a consistent bias; low r means PRISM puts the rain in the
  wrong places.
- **Gauges are not the truth.** Gauges catch less than actually fell, especially snow and
  wind-driven rain. Losses grow in wind and can reach 10% or more for snow. A PRISM total slightly
  above a high-elevation gauge, especially SNOTEL, is not necessarily wrong.
- **In-sample stations** usually agree better than independent ones. If they don't, check the
  independent gauges' locations, elevations, and observation times before trusting either group.
- **Point versus cell:** PRISM gives one value for an 800 m cell; a gauge is one point. In steep
  terrain the gauge can sit well above or below the cell's average elevation.

Example (San Juan, PRISM days Oct 20–26):
```
Event totals:
  all                    n=93   bias   -2.9 mm  MAE   7.0  RMSE  11.0  r 0.97  PRISM vs gauges -3%
  independent            n=8    bias   +1.3 mm  MAE   4.0  RMSE   5.2  r 0.99  PRISM vs gauges +1%
  SNOTEL                 n=21   bias   -4.4 mm  MAE   9.5  RMSE  13.7  r 0.95  PRISM vs gauges -3%
Daily values:
  all                    n=820  bias   -1.2 mm  MAE   3.0  RMSE   8.9  r 0.91  PRISM vs gauges -7%
Excluded from event stats: {'missing day': 43, 'days missing from record': 3}
```
Read as: across 93 gauges with mean storm totals near 115 mm, PRISM is 3% (2.9 mm) low, a typical
station differs by 7 mm, and PRISM ranks wet and dry places almost exactly (r 0.97). Daily agreement
is weaker, as expected from the day-boundary offsets. 46 stations had gaps and were not compared.

#### Runoff statistics
| Value | Meaning |
|---|---|
| Basin area (three ways) | PRISM cells inside the NLDI polygon; the NLDI polygon's own area; and USGS's published drainage area. The first two should match within ~1%. A gap to the published area means NLDI's polygon differs from USGS's official basin; volumes use the PRISM-cell area |
| Precipitation | Basin-average depth (mm) and volume (m³, acre-ft) over the gauge basin for the PRISM days |
| Total flow | All water past the gauge in the flow window: integral of 15-minute discharge, m³ |
| Event runoff | Total flow minus baseflow, the water attributable to the storm. Given as a range from the two baseflow lines below |
| Baseflow: constant | Flow held at its start-of-window value. Ignores the river staying high after the storm, so it attributes more water to the storm (upper end of the range) |
| Baseflow: line | Straight line from start-of-window to end-of-window flow. The end flow is still part of the storm's recession, so this attributes less (lower end) |
| Runoff ratio | Event runoff ÷ precipitation volume, as a range (line to constant). The share of the storm's precipitation that left the basin past the gauge within the window |
| Total flow / precipitation | Includes baseflow that would have flowed anyway; an upper bound, not a runoff ratio |
| Peak | Highest 15-minute discharge (cfs) and its time (UTC) |
| Interpolated / longest gap | Hours of the 15-minute record filled by straight-line interpolation, and the longest gap between readings |
| Approval | USGS approval of the 15-minute values. Provisional data can be revised, often around flood peaks |
| Regulation, dam storage | Upstream NID storage as a depth over the basin (mm) and as a share of this storm's precipitation; class unregulated / minor / regulated |

**Interpreting the runoff ratio:**
- **Use unregulated gauges to judge PRISM.** At a regulated gauge, reservoirs can hold back most of a
  storm (below a large dam the ratio can be near 0) or release stored water (ratio too high). Those
  ratios describe the water budget, not PRISM's accuracy. `storage_pct_of_precip` separates a gauge
  that's barely over the threshold (storage a small fraction of the storm) from one where reservoirs
  could hold the whole storm (100% or more).
- It must be well below 1 for a rain event: the rest of the precipitation soaks in, evaporates,
  or is stored as snow. A ratio near or above 1 points to PRISM underestimating precipitation,
  snowmelt adding water, or a flow window that is too long or starts on already-high flow.
- Typical values depend on the landscape. Steep mountain basins in a large storm often give roughly
  0.1–0.5; wet antecedent soils and rain on snow push ratios up. Sandy or dry soils, flat wetlands, and
  lakes can hold almost everything back: in the October 2026 test, Nebraska's Elkhorn and Florida's
  Upper St. Johns gave ratios of 0.00–0.02 for 15–40 mm of rain. Compare gauges within a region rather
  than against a fixed range.
- **Snow lowers the ratio here.** PRISM precipitation includes snow, and snow at high elevations stays
  on the ground past the flow window. In mountain basins in the cold season, read the all-precipitation
  ratio as a lower bound and the rain-only ratio as an upper bound: some of the "snow" may have fallen as
  rain (the split uses daily mean temperature), and rain on existing snow can melt it. A rain-only ratio
  above 1 means the gauge passed more water than fell as rain; in the San Juan test the highest Animas
  gauges (64–76% snow) went from 0.25–0.29 to 0.69–1.17.
- **The flow window matters.** If the recession is cut off (the run happened before `--after-days`
  had passed, or `--after-days` is short), runoff is undercounted; rerun later.

Example (`--all-gauges` on the San Juan, excerpt):
```
47 gauges with 15-minute discharge in the study area's bounding box; 144 NID dams in the study basin
  09342500 SAN JUAN RIVER AT PAGOSA SPRINGS, CO            727 km²   174.9 mm  ratio 0.35–0.42 (rain-only 0.43–0.50, 17% snow)  unregulated (0.2 mm storage, 0% of precip)
  09355500 SAN JUAN RIVER NEAR ARCHULETA, NM             8,379 km²   137.1 mm  ratio 0.01–0.01 (rain-only 0.01–0.01, 7% snow)  regulated (274.3 mm storage, 200% of precip)
  09358000 ANIMAS RIVER AT SILVERTON, CO.                  182 km²   136.3 mm  ratio 0.27–0.29 (rain-only 1.06–1.17, 75% snow)  minor (1.1 mm storage, 1% of precip)
  09361500 ANIMAS RIVER AT DURANGO, CO                   1,765 km²   124.7 mm  ratio 0.26–0.30 (rain-only 0.39–0.45, 32% snow)  regulated (16.8 mm storage, 13% of precip)
  09365000 skipped: discharge gap of 50.2 h (limit 12 h)
PRISM days 2026-10-20 to 2026-10-27: 29 gauges compared {'regulated': 13, 'unregulated': 12, 'minor': 4}; 10 skipped (reasons in runoff_summary.csv)
```
(Release-2 precipitation; flow recorded to Oct 31, before the end of the 7-day recession window, which
later runs fill in.) Read as: at Pagosa Springs (unregulated, a clean PRISM check) about a third of the
storm's 175 mm had passed the gauge by the end of the record, and 17% of the basin's precipitation was
snow by the default split, so the true ratio is likely between 0.35 and 0.50. At Silverton, 75% of the
precipitation is classed as snow, and the rain-only ratio above 1 says part of it fell as rain or rain
melted earlier snow; `validate_snow.py` finds the split runs about twice the observed snow near
Silverton. Below Navajo Dam (Archuleta), reservoir storage is twice the storm and the ratio is ~0: the
dam held the flood. Durango was labeled regulated in this run by capacity alone (Electra Lake); with
effective storage (added since) it is minor, 6.2 mm, and its ratio matches the unregulated Animas
gauges upstream. Farmington's record has a 50-hour gap during the flood, so it is skipped. Each gauge's
`runoff_<site>.json` has the full detail: the three basin areas, daily precipitation, flow volumes,
peak time, interpolated hours, and approval status.

#### Field reference
**`stations`** (one row per gauge; storm totals):
| Field | Meaning |
|---|---|
| station, network, ghcn_id, state | Gauge name, network (COOP, CoCoRaHS, ASOS/first-order, SNOTEL, RAWS, or other), GHCN-Daily id, and state |
| elev_m | Gauge elevation (m) |
| obs_hour | Usual observation hour, local standard time (24 = midnight) |
| max_offset_h | Largest gap between the gauge's day end and the matched PRISM day end, hours (+ = gauge day ends later) |
| aligned | True when every day's offset is within `--max-offset` |
| inside_basin | Gauge inside the basin polygon (false = in the buffer) |
| in_sample | Gauge within `--match-km` of a station PRISM used on any day in the window |
| obs_mm, prism_mm | Storm totals from the gauge and from PRISM at the gauge's cell |
| diff_mm | prism_mm − obs_mm |
| ratio | prism_mm ÷ obs_mm |
| status | `ok`, or why there is no comparison (see below) |
| in_stats | True when the row is counted in the statistics (status ok and aligned) |
| lon, lat | Gauge location (NAD83) |

**`station_days`** (one row per gauge per PRISM day):
| Field | Meaning |
|---|---|
| prism_day | PRISM day label (the day ending 12:00 UTC on this date) |
| period_start, period_end | The PRISM day's UTC start and end (used by the Temporal Controller) |
| obs_date | Date the gauge reported for |
| obs_hour, hour_known | Hour the gauge's day ended; hour_known false when ACIS had no hour and the station's usual hour (or a network default) was used |
| offset_h | Gauge day end minus PRISM day end, hours |
| kind | `value`, `trace` (counted as 0), `missing`, `subsequent` (included in a later accumulated total), or `accumulated` |
| obs_mm, prism_mm, diff_mm | Gauge, PRISM, and PRISM − gauge for the day (diff only when compared) |
| in_stats | Counted in the daily statistics (a reading, not accumulated, offset within `--max-offset`) |
| station, network, ghcn_id, in_sample, inside_basin, lon, lat | As in `stations` |

**`status` values in `stations`:**
| Status | Meaning |
|---|---|
| ok | Complete storm total compared |
| missing day | The gauge has no report for at least one day in the window |
| days missing from record | No gauge report lines up with some PRISM day in the window (for example the record ends early) |
| observation time changed in the window | The gauge's reading hour changed mid-window (for example 7 AM to midnight), so two readings fall on one PRISM day and none on another |
| accumulation starts before window / ends after window | A multi-day total straddles the window edge, so the in-window share is unknown |
| no PRISM value | The gauge's cell has no PRISM data |
| day ends too far from PRISM day | Complete total, but the gauge's day ends more than `--max-offset` hours from the PRISM day |

**`gauge_basins` and `gauges`** (one row per USGS gauge; `gauge_basins` has compared gauges only,
`gauges` every gauge in the study basin; also `runoff_summary.csv`):
| Field | Meaning |
|---|---|
| site, name | USGS site number and name |
| status | `ok`, or why the gauge was not compared (see below) |
| regulation | `unregulated`, `minor`, or `regulated` (empty when not compared) |
| dam_storage_mm, dam_storage_af | Upstream NID capacity as a depth over the basin, and in acre-ft |
| dam_effective_mm | Effective storage as a depth over the basin: each dam's capacity capped by the storm over its own drainage area; sets the class |
| storage_pct_of_precip | dam_effective_mm as a percentage of the storm's precipitation depth |
| n_dams, largest_dams | Number of upstream NID dams; the three largest with storage |
| prism_start, prism_end | PRISM days of the precipitation window |
| area_km2, usgs_area_km2 | Basin area from PRISM cells; USGS published drainage area |
| precip_mm, precip_m3 | Basin precipitation depth and volume |
| total_flow_m3 | All flow past the gauge in the flow window |
| runoff_line_m3, runoff_const_m3 | Event runoff with the line and constant baselines |
| ratio_low, ratio_high | Runoff ratio range, low to high |
| ratio_line, ratio_const | Runoff ratio with each baseline (the line baseline gives the lower value unless flow was falling at the start of the window) |
| peak_cfs, peak_utc | Peak 15-minute discharge and its time |
| rain_mm, snow_pct | With `tmean`: basin rain depth and the share of precipitation classed as snow |
| rain_ratio_low, rain_ratio_high | With `tmean`: runoff ÷ rain volume (empty when all of it was snow) |
| interp_h | Hours of the 15-minute grid with no reading within 7.5 minutes (an hourly gauge shows 3 of every 4 steps) |
| flow_end_utc | End of the flow record used (earlier than requested if the run came before `--after-days` had passed) |
| flow_complete | False when the flow record ends before the requested window does; later runs refetch it once more flow exists |
| lon, lat | Gauge location |

**`status` values for gauges:**
| Status | Meaning |
|---|---|
| ok | Compared |
| discharge gap of N h | The 15-minute record has a gap longer than `--max-gap-hours` in the window (common at flood peaks when a gauge is damaged or values are withheld) |
| No discharge values in the flow window | USGS returned no readings for the window |
| gauge basin lies mostly outside the study basin | Less than half of the gauge's drainage basin is in the study basin (a gauge just over the divide) |
| N% of the gauge basin is outside PRISM coverage | Part of the gauge basin is in Canada or Mexico (more than `--max-missing-pct`) |
| no NLDI basin | NLDI has no basin for the site (saved; a request that fails is not saved and is retried next run) |
| request failed | A USGS service error; rerun later |
| no precipitation over the gauge basin in the window | Nothing to compare runoff with (a dry window) |
| site type is …, not a stream | A canal, ditch, or other non-stream site |

**`dams`** (NID dams in the study basin): `nidid`, `name`, `river`, `normal_storage_af`,
`nid_storage_af`, `storage_af` (the value used), `purposes`, and `year`.

**`totals.gpkg` › `basin_total_<start>_<end>`** (one row): `name`, `huc`, `prism_start`, `prism_end`, `area_km2` (with
PRISM data), `basin_cells_km2` (all cells in the basin), `coverage_pct`, `mean_mm`, `max_mm`, `volume_m3`,
`volume_km3`, `volume_acre_ft`, `subbasin_level`, `subbasins`, and `res` (cube resolution).

**`snotel.csv` / `snotel` layer** (one row per SNOTEL station): `triplet`, `name`, `elevation_m`,
`inside_basin`, `lon`, `lat`, `status` (`ok` or why skipped), `precip_mm` (station), `prism_precip_mm`
(at the station's cell), `swe_before_mm`, `swe_gain_mm` (peak in the window minus before), `swe_net_mm`
(end minus before), `station_split_snow_mm`, `prism_snow_mm`, `observed_snow_pct`,
`station_split_snow_pct`, `prism_snow_pct`, and `station_mean_tavg_c`.

**`totals.gpkg` › `subbasins_<start>_<end>`** (one row per unit; also `subbasin_totals_<start>_<end>.csv`): `huc`, `level` (e.g.
HUC10), `name`, `states`, `area_km2` (with PRISM data), `coverage_pct`, `mean_mm`, `max_mm`, `volume_m3`,
`volume_acre_ft`, `share_pct` (of the basin's volume), `rank` (1 = most water), `prism_start`, and `prism_end`.

**`runoff_<site>_flow.csv`:** `time_utc`, `q_cfs` (15-minute discharge after gap filling),
`interpolated`, `baseflow_constant_cfs`, and `baseflow_line_cfs`. Plot `q_cfs` against both baselines to
see the event runoff the ratio is built from.

### Mapping the comparison in QGIS
Drag `validation.gpkg` into QGIS and add all layers (the styles load automatically), or build a full
project with `build_qgis_project.py`. The [Map guide](#map-guide-layers-symbols-and-how-they-relate-to-prism)
explains every layer and symbol.

## `report.py`: storm report with charts

```bash
python scripts/report.py --out data/huc<HUC>                       # window of the latest storm_total.py run
python scripts/report.py --out data/huc<HUC> --start <YYYY-MM-DD> --end <YYYY-MM-DD> --gauges 09342500 09361500
```
Writes `events/<event>/report/storm_report_<start>_<end>.html`, one page with no external dependencies (open it
in any browser, email it, or put it on a share), and each chart as an SVG in the event's `report/figures/` for
slides and documents. The page follows the viewer's light or dark setting, shows values on hover, and
has a data table under each chart.

| Flag | Default | Notes |
|---|---|---|
| `--start` / `--end` | the first event in `storm.toml`, else the latest `storm_total.py` window, else the whole cube | PRISM days |
| `--keep-previous` | off | Move an existing report for the window to its `report/history/` first (`pipeline.py update` does) |
| `--gauges` | up to 3 gauges not regulated by dams, with the most event runoff | USGS sites for the storm timeline |
| `--title` | the basin name | Page title |
| `--res` | `800m` | or `4km` |

| Section | Shows | Needs (same window) |
|---|---|---|
| Headline tiles | Storm volume with its range, basin mean and max, PRISM releases, percent of normal, snow share, and QC status | `storm_total.py` (range), `storm_context.py` (normal), `tmean` (snow) |
| Storm timeline | Basin precipitation per PRISM day (rain and snow) above 15-minute discharge at the chosen gauges, event runoff shaded above the baseflow line | `validate_runoff.py` for the gauges |
| PRISM vs rain gauges | Storm totals, PRISM vs gauge, with a 1:1 line; PRISM inputs and independent gauges marked; bias, r, and RMSE | `validate_stations.py` |
| Where the water went | Runoff ratio range per gauge, largest basin first, colored by regulation, with the rain-only ratio | `validate_runoff.py` |
| Volume and its uncertainty | Each uncertainty component and the total range as bars | `storm_total.py` |
| Snow check | Snow share by elevation at SNOTEL stations, observed vs PRISM split | `validate_snow.py` |

A section whose inputs are missing, or were made for another window, is replaced by a note naming the
command to run. Rerun the report after a refresh (it reads the outputs; nothing is downloaded).

## `build_qgis_project.py`: an organized QGIS project
```bash
python scripts/build_qgis_project.py --out data/huc<HUC>              # writes data/huc<HUC>/qgis/huc<HUC>.qgz
python scripts/build_qgis_project.py --out <folder> --name My_Storm    # writes <folder>/qgis/My_Storm.qgz
```
Finds the outputs in `--out` by their standard names and writes a styled, organized project to
`<out>/qgis/`. Run it after the other tools; rerun it to rebuild (the previous project is kept as
`.qgz.bak`, so edit a copy if you customize it).

| Group | Layers (on by default in bold) |
|---|---|
| Storm: *event* — *dates* (one per storm window) | **Totals:** **basin label** (volume, depth and uncertainty range for the window), **precipitation depth (mm)**, water volume per cell, rain depth, snow-water, percent of normal, precipitation by subbasin. **Validation:** **USGS gauges** (by regulation, runoff ratio labels), **rain gauges: PRISM − gauge storm total**, rain gauges daily (Temporal), gauge drainage basins, SNOTEL snow share. The first window's group is on; the others are off |
| Other windows | Totals for windows that aren't storms, such as the whole download window or an earlier one-off `storm_total.py --tif` (group off) |
| Daily precipitation | One raster per PRISM day, each tied to its 24-hour period for the Temporal Controller (group off) |
| Boundaries and dams | **Study basin** outline; dams (NID) |
| Basemap | **OpenStreetMap** |

**Several storms in one basin.** The storm windows are the events in `storm.toml`, else every window
`storm_total.py` analyzed (one uncertainty file each), else, for folders made before either existed, the
event total for the subbasin window (or the longest). Each window's layers come from the window-named
GeoPackage layers the validation and subbasin scripts write (`stations_<start>_<end>` and so on), so
running a second storm never replaces the first storm's map layers; a plain layer from an older run is
used for the window it was made for. Rasters are matched to a window by the dates at the end of their
names, so older names (`rain_mm_<start>_<end>.tif`) work too. A storm listed in `storm.toml` that hasn't
been run yet is left out until it has outputs.

- **Layers and symbols** are explained in the [Map guide](#map-guide-layers-symbols-and-how-they-relate-to-prism).
- **Basin label:** volume, depth, and window, plus the uncertainty range when `storm_total.py` has run for
  the same window as `subbasin_totals.py`.
- **Colors:** daily rasters share one scale, and each kind of total (depth, rain, snow, volume, subbasin
  depth) shares one scale across every storm window, so a color means the same depth in each storm; classes
  are round numbers spread on a log scale from the data's minimum to its maximum; areas below the first
  class are transparent. Station difference classes are also
  set from the data, so small storms aren't all one color.
- Only one resolution is mapped (800 m if present), only precipitation (not `tmean`), and only tifs whose
  names say what they hold (`depth`/`total`, `volume`/`m3`, `rain`, `snow`, or `pct`/`normal`).
- **Map themes**, one set per storm window (named `<event>: <theme>` when there are several; without the
  prefix when there is one): Storm total; Validation (rain gauges and river gauges over the storm total);
  Rivers and dams (gauges, their drainage basins, and dams); Volume by subbasin; Rain and snow (rain and
  snow-water rasters with the SNOTEL comparison); Percent of normal (from `storm_context.py --pct-tif`);
  and Daily animation (Temporal Controller). Applying a theme switches to that storm's group. Pick one from
  the Map Themes button in the Layers panel.
- **Temporal Controller:** preset to the storm window with a 1-day step. Turn on the Daily
  precipitation group (or apply the Daily animation theme) and press play.
- **Only precipitation is mapped:** `tmean` (and other variables) in the same folder are left out.
- **Also set:** a NAD83 UTM CRS for the basin, relative paths (move or share the whole folder), a basin
  bookmark, and layer metadata with sources and PRISM release numbers.
- **QGIS version:** runs under QGIS's own Python. From the conda environment it relaunches itself with
  the newest QGIS 3.x installed (on macOS; for example 3.44 LTR), falling back to the newest QGIS, and says
  which one. QGIS 4 opens projects saved by QGIS 3.44 (checked with 4.2.3), but QGIS 3 may not open
  projects saved by QGIS 4. Use `--qgis-python` or `QGIS_PYTHON` to pick another.

## Map guide: layers, symbols, and how they relate to PRISM

### How the pieces fit together
PRISM is the thing being examined: a model that estimates daily precipitation on an 800 m grid from
gauges, terrain, and climate patterns. Everything else on the map is there to check it or explain it.

| Layer | What it is | Source | Role relative to PRISM |
|---|---|---|---|
| Daily precipitation, storm totals | PRISM's estimate per 800 m cell, per PRISM day and summed over the window | PRISM (Oregon State University) | **The data being evaluated** |
| Rain gauges (`stations`, `station_days`) | Precipitation measured at a point by COOP, CoCoRaHS, airport, and SNOTEL gauges | RCC-ACIS | **Direct check:** PRISM at the gauge's cell vs what the gauge caught |
| USGS gauges (`gauges`) | River discharge measured every 15 minutes | USGS Water Data API | **Indirect check:** does the water that left the basin make sense for the precipitation PRISM put on it? |
| Gauge drainage basins (`gauge_basins`) | The land that drains to each USGS gauge | USGS NLDI | The area PRISM precipitation is summed over for that gauge's runoff ratio |
| Dams (`dams`) | Dams and their storage capacity | National Inventory of Dams | **Context only:** they decide whether a river gauge's runoff ratio says anything about PRISM |
| Rain and snow-water rasters | PRISM precipitation split into rain and snow by PRISM daily mean temperature | PRISM `ppt` and `tmean` (`storm_total.py`) | **Derived from the data being evaluated** |
| SNOTEL (`snotel`) | Snow-water gained by snow pillows over the window | NRCS AWDB | **Check on the rain/snow split,** not on PRISM precipitation |
| Precipitation by subbasin (`subbasins`) | PRISM depth and volume summed over each HUC10 or HUC12 | PRISM over WBD units (`subbasin_totals.py`) | **The data being evaluated,** aggregated to show where the water fell |
| Study basin | The watershed the run was built for | USGS Watershed Boundary Dataset | The boundary PRISM was clipped to; its label gives the storm's total volume |

**Two kinds of check, different questions:**
- A rain gauge asks *"did PRISM get the amount right at this spot?"* It compares millimetres with
  millimetres at one point.
- A river gauge asks *"is PRISM's basin total plausible?"* It compares the volume of water that ran
  off with the volume PRISM says fell. Most rain never reaches the river (it soaks in, evaporates, or
  stays as snow), so the runoff ratio is a fraction, not a match. It only tests PRISM where no dams
  intervene.

### Precipitation rasters
| Layer | Shows | Symbology |
|---|---|---|
| Precipitation depth (mm), *window* | PRISM storm total per cell | Pale yellow → green → blue → dark navy as depth increases, in round-number classes from the storm's minimum to maximum |
| Precipitation (mm), PRISM day *date* | One PRISM day per layer | Same colors, on a separate scale shared by all days |
| Water volume per cell (m³) | Depth × cell area (from `storm_total.py --volume-tif`) | Continuous yellow-green-blue, low to high |
| Precipitation by subbasin (*HUC level*) | Each subbasin's mean depth (fill) and water volume (labels) | Fill uses the same yellow-to-navy colors as the depth rasters, with classes from the subbasins' range; the 12 largest subbasins by volume are labeled with acre-ft and their share of the basin |

- **Read the legend, not just the color.** Class breaks are set from each run's data, so the same
  blue can mean 50 mm in one project and 300 mm in another. Daily layers and storm totals also use
  separate scales, so a daily layer and the storm total aren't color-comparable.
- **Transparent cells mean one of two things:** the value is below the lowest class in the legend, or
  there is no PRISM data at all. Classes run from the storm's minimum to its maximum, so in a storm that
  wet the whole basin (the San Juan's lowest cell got 29 mm, so classes start at 25 mm) nothing is
  transparent except no-data areas; where some cells stayed dry, the lowest class starts at 0.5–1 mm. PRISM covers only the contiguous US, so
  basin area in Canada or Mexico is empty inside the basin outline. Click a cell with Identify to tell
  them apart: dry cells return a small number, no-data cells return nothing.
- **Dates are PRISM days.** "PRISM day Oct 23" is the 24 hours ending 12:00 UTC on Oct 23 (in late
  October: 5 AM PDT, 6 AM MDT, 7 AM CDT, or 8 AM EDT). Layer time ranges are stored in UTC.
- PRISM values are cell averages over about 0.7 km² (an 800 m cell); a point can get more or less.

### Rain gauges: `stations` (storm total) and `station_days` (per day)
| Symbol property | Meaning |
|---|---|
| **Color** | PRISM minus gauge: **brown = PRISM drier** than the gauge, **near-white = close agreement**, and **teal = PRISM wetter**. Class breaks are in mm and shown in the legend |
| **Shape** | **Circle = in-sample** (PRISM used this gauge or one within 0.5 km, so agreement is expected); **square = independent** (a fairer test) |
| **Size** | Grows with the gauge's own total (bigger = more rain fell there) |
| **Small gray ring** | Not compared: a missing day, a multi-day report straddling the window, or a reporting time too far from PRISM's day (hover for the reason) |

- In a project from `build_qgis_project.py` the color classes come from that run's differences (for
  example ±1/2/5 mm in a small storm). Dragging `validation.gpkg` in directly uses fixed classes
  (±3/10/25 mm storm totals, ±1/5/15 mm daily), which suit large storms.
- Stations can appear **outside the basin outline**: the comparison includes gauges within
  `--buffer-km` (default 10 km) of the basin, because PRISM is sampled from the national grid there too.
- **Gauges are not the truth.** They catch less than falls, especially snow and wind-driven rain, so
  a teal SNOTEL site (PRISM wetter) isn't necessarily a PRISM error.
- `station_days` is off by default; turn it on with the Temporal Controller to step through the
  storm day by day. Without the Temporal Controller all days draw on top of one another.

### USGS stream gauges: `gauges` and `gauge_basins`
| Symbol | Meaning |
|---|---|
| **Navy triangle**, solid navy basin outline | **Unregulated:** upstream dam storage under 1 mm over the basin. The runoff ratio is a fair check on PRISM |
| **Steel-blue triangle**, dashed steel-blue outline | **Minor regulation:** 1–10 mm of storage. Usually still a reasonable check |
| **Orange triangle**, dashed orange outline | **Regulated:** 10 mm or more. Reservoirs can store or release water, so the ratio describes dam operations as much as precipitation |
| **Gray outline triangle** | Not compared (no basin outline): gaps in the flow record; a canal, spring, or tidal site; or a basin mostly outside the study basin or outside PRISM coverage. Hover for the reason |
| **Label** | Gauge name and the runoff ratio range, low to high (from the two baseflow methods); "(regulated)" when regulated |

- **"Storage in mm"** is reservoir capacity spread evenly over the gauge's basin, so it compares
  directly with storm precipitation depth. 274 mm of storage against a 140 mm storm (San Juan below
  Navajo Dam) means the reservoirs could hold the whole storm. Hover for `storage_pct_of_precip`.
- **Basins nest.** A downstream gauge's basin contains every upstream one, so outlines overlap. Turn
  `gauge_basins` on with only a few gauges in view, or use Identify to see which basin is which.
- **Gauge basins can extend past the study basin outline.** NLDI and WBD boundaries differ, especially
  in flat terrain; precipitation is still taken from PRISM for the whole gauge basin.
- **A runoff ratio near 0** isn't necessarily wrong: dry or sandy soils, wetlands, and lakes can absorb
  a whole storm, and snow stays put. Compare gauges within a region rather than against a fixed number.
- The gauge layer and the rain gauges are **different networks**: triangles measure rivers, circles
  and squares measure rain.

### Dams: `dams`
| Symbol | Meaning |
|---|---|
| **Brown square** | A dam in the National Inventory of Dams, inside the study basin |
| **Size** | Storage capacity on a log scale: a 10 acre-ft stock pond is a speck, a 1,000,000 acre-ft reservoir is the largest symbol |
| **Label** | Name and storage, for reservoirs of 20,000 acre-ft or more |

- Storage is capacity (normal storage, else maximum), not how full the reservoir was during the storm.
- Each reservoir is counted once (dikes and secondary dams listed with the same storage are merged).
- Dams aren't compared with PRISM; they explain why some river gauges are labeled regulated.
- Off-channel reservoirs filled by pumping (for example Lake Nighthorse) and transbasin diversions
  (for example the San Juan–Chama Project) count as storage even though they barely regulate storm flow.

### SNOTEL snow share: `snotel`
| Symbol | Meaning |
|---|---|
| **Triangle color** | PRISM's snow share minus the observed share (SWE gain ÷ station precipitation), in percentage points. Teal: PRISM's split has more snow; brown: less. Classes are set from the data |
| **Size** | Snow-water gained over the window |
| **Gray outline** | Skipped (missing SNOTEL data) |

- Observed snow is a lower bound (melt during the window), so small positive differences are expected.
- Attributes include all three shares (`observed_snow_pct`, `station_split_snow_pct`, and `prism_snow_pct`).

### Total volume: study basin label and subbasins
- **The study basin label** gives the storm's headline numbers on separate lines, for example *"San Juan:
  4.68 km³ = 3.80 million acre-ft / mean 73 mm, max 228 mm, PRISM days 2026-10-20 to 2026-10-27 / range
  4.65–4.92 km³"*, plus the share of the basin without PRISM data when there is any (14.3% for the Upper
  Skagit, in Canada). The range appears when `storm_total.py` has written the uncertainty for the same
  window and resolution.
- **Subbasins are colored by depth, not volume.** Volume grows with area, so coloring by volume would
  mostly show which units are biggest. Depth (mm) shows how wet each subbasin was; the labels give the
  volume. A large, moderately wet subbasin can hold more water than a small, very wet one.
- **`share_pct`** is the subbasin's share of the basin's total volume; shares add up to 100%.
- **Blank subbasins** inside the basin outline have no PRISM data (for example Canadian HUC12s).
- Hover for depth, volume in acre-ft and m³, share, rank, and coverage. The **Volume by subbasin** map
  theme shows just this layer with the basin label.

### Boundaries and basemap
- **Study basin:** pink/magenta outline: the USGS WBD watershed PRISM was clipped to. With
  `subbasin_totals.py` output, this layer is `basin_total` and carries the storm totals.
- **OpenStreetMap:** reference basemap; needs an internet connection to draw.

### Getting values on the map
- **Map tips** (View › Show Map Tips) show each station's, gauge's, or dam's values on hover: totals,
  differences, ratios, storage, and why a feature wasn't compared.
- **Identify** (the i tool) shows every field. The fields are listed under
  [Field reference](#field-reference).
- **Map themes** (in a built project, the Map Themes button in the Layers panel): Storm total,
  Validation, Rivers and dams, Volume by subbasin, Rain and snow, Percent of normal, and Daily animation;
  one set per storm window, prefixed with the event name when the folder has several.
- **Units:** precipitation in mm (inches in printed summaries and CSV columns ending `_in`); river flow
  in cfs; volumes in m³ (acre-ft in printed summaries); and dam storage in acre-ft and mm over the basin.

### Opening outputs by hand
- GeoTIFFs open directly; use **Singleband pseudocolor** for depth and volume rasters.
- NetCDFs open as rasters (one band per day; QGIS shows the first three as RGB by default, which
  is not meaningful here) or, for the Temporal Controller, as mesh layers via
  **Layer › Add Layer › Add Mesh Layer**. Set a fixed min/max in Symbology so days are comparable.
- The NetCDFs follow CF-1.10: each time stamp is 00:00 UTC on the PRISM day label, the midpoint of
  the day, with `time_bnds` giving the 24 hours it covers (12:00 UTC the day before to 12:00 UTC);
  precipitation has `standard_name` `lwe_thickness_of_precipitation_amount` and `cell_methods`
  `area: mean time: sum`, and `cell_area` is linked as the cell measure. The cube also carries each
  day's PRISM `release`.

## Notes
- **PRISM day:** 12:00 UTC to 12:00 UTC, labeled with the end date. For example, in Mountain Daylight
  Time the Oct 23 grid covers 6 AM Oct 22 to 6 AM Oct 23. Pad storm windows by a day on each side.
- **Download limit:** PRISM allows each file to be downloaded only twice per 24 hours per IP. The
  script keeps every zip in the PRISM store and never re-requests one it has.
- **PRISM store:** the grids are national, so every basin shares one store of zips, unzipped grids and
  normals: `prism_store/` next to the basin folders, or `prism_store` in `storm.toml` (relative to it),
  `PRISM_STORE`, or `--store` (`prism_cache`, `PRISM_CACHE` and `--cache` are the older names). Each file is
  downloaded once for all basins. Unzipped grids are kept per release, and each basin reads the release its
  own clipped days were made from, so one basin's refresh doesn't change another's numbers until that
  basin is updated too (it then takes the newer release without a download). Older releases are unzipped
  again from `releases/` when a basin still needs one.
- **Revisions:** each daily grid is released 8 times over about 6 months (the day after, ~4 days
  later, and then about monthly). The CSV's `release` column and the cube's `release` variable show which
  one you have. Use `--refresh` to update (see Updating to newer PRISM releases).

## Troubleshooting
- **`... is in use by another run`:** another run is writing to the same folder (it holds `<out>/.lock`).
  Wait for it; if that run is gone (the machine restarted), delete the `.lock` file. Locks left by a
  process that no longer exists on this machine are taken over automatically.
- **A step fails with a network error (exit 4):** run `python scripts/check_endpoints.py` to see which
  service isn't answering, then rerun; finished work is kept. The run's log in `<out>/run/logs/` has each
  request.
- **`CERTIFICATE_VERIFY_FAILED` from services.nacse.org:** the server's chain ends at emSign Root
  CA - G1, which Python's certifi bundle lacks. Install `truststore` (in `requirements.txt`) so the
  script uses the operating system's trust store. Don't disable verification.
- **`CRSError: The EPSG code is unknown … another PROJ installation`:** a `PROJ_DATA` variable points
  at an older PROJ database (e.g. from Anaconda) than the one rasterio was built with. Use the
  `prism-storm-analysis` conda environment (it sets `PROJ_DATA` correctly when activated) or `.venv`.
  As a one-off workaround, prefix commands with `env -u PROJ_DATA`.

## Roadmap
What is built and what comes next (comparison with other precipitation products, storm return periods,
rainfall timing, and maps in the report) are described in [docs/ROADMAP.md](docs/ROADMAP.md).

## Tests
```bash
python -m pytest tests -q
```
Offline only: synthetic rasters stand in for PRISM downloads, and any request goes to a closed local
port (`PRISM_STORM_RETRY_WAIT=0` keeps the retries of those failed requests fast). `test_pipeline.py`
runs the pipeline end to end, interrupts a run with Ctrl-C and resumes it, and recovers from files a
crash could leave. `test_build_qgis_project.py` also builds a real project when QGIS is installed (skipped otherwise).
`.github/workflows/tests.yml` runs the suite on GitHub once the repository is pushed there.

Data: PRISM Group, Oregon State University, https://prism.oregonstate.edu

## License
MIT; see [LICENSE](LICENSE). PRISM data are subject to the PRISM Group's own terms of use.
