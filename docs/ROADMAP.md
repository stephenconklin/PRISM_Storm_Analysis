# Roadmap: toward an industry-standard storm precipitation tool

Planned enhancements for PRISM Data Wrangler, written to be picked up and built one at a time.
Each item lists the goal, why it matters, an approach, data sources, outputs, and what "done" means.

Endpoints and dataset details below are starting points from planning, not verified integrations.
Per `CLAUDE.md`, check every external endpoint before building on it.

Effort: **S** (an afternoon), **M** (a few days), **L** (a week or more).

## Where the tool stands (1 Nov 2026)

| Area | Today |
|---|---|
| Download / clip | `prism_watershed.py`: any HUC or polygon, any PRISM variable, cached zips, `--refresh` to newer PRISM releases with a per-release history |
| Totals / volume | `storm_total.py`: any date window; depth, ellipsoidal per-cell volume, rain/snow split, revision change, boundary sensitivity; GeoTIFF / NetCDF outputs; `subbasin_totals.py` per HUC10/12 |
| Lookup | `find_huc.py`: WBD name search |
| QC | `qc.py`: nine checks after `prism_watershed.py`, five after `storm_total.py`; `--qc-strict` |
| Uncertainty | `uncertainty.py`: volume with a bounding range (gauges, boundary, revisions); other products not yet assessed |
| Validation | `validate_stations.py` (1.1), `validate_runoff.py` (1.2, with rain-only ratios), `validate_snow.py` (SNOTEL check of the rain/snow split); 1.3 not started |
| Provenance | `run_manifest.json` (one entry per run); releases in the CSV, clipped tifs and cube; release history CSV |
| Engineering | Git (no tags yet); offline pytest, including a real QGIS build when QGIS is installed; no packaging, logging or CI |

## Multi-basin test (31 Oct 2026)

The full workflow ran on three contrasting basins, reusing the national Oct 20–27 PRISM zips (no new
downloads), plus the San Juan. Outputs are in `/Volumes/ConklinGeospatialData/Data/PRISM_data/`
(`Upper_Skagit_test`, `Elkhorn_test`, `Upper_St_Johns_test`).

| Basin (HUC) | Tests | Stations: storm totals | Gauges compared / skipped |
|---|---|---|---|
| Upper Skagit, WA/BC (17110005) | Canadian border, Pacific time, dams, snow | n=6, PRISM −6%, r 0.84 | 8 / 2 (main stem: basin 20–24% in Canada) |
| Elkhorn, NE (102200) | Central time, 6-digit HUC, plains | n=32, +2%, r 0.99 | 9 / 1 (canal) |
| Upper St. Johns, FL (03080101) | Eastern time, flat wetlands, canals, springs, tidal | n=74, +2%, r 0.96 | 22 / 6 (springs, tidal, canals) |

Fixed as a result: gauge-basin precipitation now comes from the national grids, so NLDI basins can
extend past the study basin (St. Johns went from 15 to 22 gauges); a warning when part of the basin has
no PRISM data (Skagit: 14%, in Canada), with clear skip reasons; runoff ratio ranges shown low to high;
gauges whose basins lie mostly outside the study basin are skipped; `--gauge` runs merge into
`runoff_summary.csv`.

**Done since:** data-driven map classes (in the QGIS builder) and region-aware runoff guidance (README).

**Still open:**
- SNOTEL lag in ACIS removes SNOTEL from windows ending within a few days of the run (Skagit had none).

---

## Suggested order

1. ~~**QC checks and run manifest** (sections 2 and 5.1)~~ — built 1 Nov 2026.
2. ~~**Uncertainty: revision and boundary sensitivity** (3.1, 3.2)~~ — built 1 Nov 2026.
3. ~~**Station validation** (1.1)~~ — built; follow-ups listed in 1.1.
4. ~~**Rain/snow partition** (4)~~ — built 1 Nov 2026, with the SNOTEL check.
5. ~~**Streamflow comparison** (1.2)~~ — built; ~~**uncertainty roll-up** (3.3)~~ — built 1 Nov 2026 without
   product spread; next **multi-product comparison** (1.3), which feeds 3.3.
6. **Storm context** (6) and ~~**reports** (7)~~ (built 1 Nov 2026; maps and PDF still open).
7. **Engineering** (8). Git is started; add CI and release tags at any point, the rest as the tool grows.

---

## 1. Validation against independent observations

### 1.1 Station comparison (bias, RMSE, correlation) — M
> **Status: built 31 Oct 2026** as `scripts/validate_stations.py` (network code in `observations.py`),
> with a styled GeoPackage for QGIS (`stations`, `station_days` with Temporal Controller fields).
> Decisions: RCC-ACIS is the only source (it includes SNOTEL via GHCN `USS…` ids, so AWDB wasn't needed);
> PRISM is sampled from the national grids in `extracted/` (nearest cell) so buffer stations work;
> each station day is matched to the PRISM day with the closest end time, max offset 6 h; in-sample =
> within 0.5 km of a `stn.csv` station.
>
> First result (San Juan, HUC 1408, PRISM days Oct 20–26, basin + 10 km): 93 storm totals, PRISM
> 3% below gauges, r 0.97, RMSE 11 mm; SNOTEL (21) −3%, CoCoRaHS (60) −2%, COOP (8) −5%.
> Only 8 independent gauges: almost every gauge in ACIS was a PRISM input.
>
> **Follow-ups:**
> - More independent gauges: networks PRISM lists but ACIS lacks (MesoWest/Synoptic, COAGMET) or
>   gauges PRISM didn't use that day.
> - SNOTEL directly from NRCS AWDB to avoid the ACIS lag (window had to end Oct 26).
> - Bias vs elevation (table and chart): PRISM's terrain adjustment is the most likely error source.
> - Optional bilinear sampling; flag gauges whose 800 m cell spans a large elevation range.

**Goal:** Compare gridded daily precipitation with gauge observations in and near the basin for the
storm window.

**Why:** Gives a measured error for this storm, not a generic accuracy figure.

**What we have:** Each PRISM zip has `*.stn.csv`, the stations used for that day's grid: `Station`,
`Name`, `Longitude`, `Latitude`, `Elevation(m)`, `Network`, `station_id` (about 21,800 stations
nationally for 2026-10-23). It does **not** include observed values. It is still useful for:
- station density in and around the basin (QC, section 2)
- choosing which gauges to compare against, and knowing they were inputs to PRISM

**Observed values (verify before use):**
- GHCN-Daily (NOAA NCEI): COOP and CoCoRaHS daily precipitation
- RCC-ACIS web services (`data.rcc-acis.org`): JSON access to daily station data, convenient for basin queries
- NRCS SNOTEL (AWDB): high-elevation precipitation and snow water equivalent

**Approach:**
1. Select stations within the basin plus a buffer (for example 20 km).
2. Fetch daily totals for the window; align each station's observation day with the PRISM day
   (12:00 UTC to 12:00 UTC). COOP observation times vary, so record each station's time and flag mismatches.
3. Sample the PRISM grid at each station (nearest cell; optionally bilinear).
4. Report per station and overall: bias, mean absolute error, RMSE, correlation, and the event-total
   ratio (PRISM ÷ observed).
5. Stations listed in `stn.csv` were PRISM inputs, so comparing against them is in-sample and
   optimistic; label it that way. A fairer check uses stations PRISM did not use (in a network but
   missing from that day's `stn.csv`). Re-running PRISM without a station (leave-one-out) isn't possible here.

**Outputs (as planned; built as `stations.csv`, `station_days.csv`, `stations_summary.json`):** `validation/stations.csv` (per station, per day), `validation/summary.json`, and a
map of station bias.

**Done when:** a run prints N stations, bias, RMSE and the event-total ratio, and writes the files
above. Tests use a synthetic grid plus fake station data.

### 1.2 Streamflow volume and runoff ratio — M
> **Status: built 31 Oct 2026** as `scripts/validate_runoff.py`. Decisions: USGS Water Data API
> 15-minute `continuous` values (daily values were null on the peak days, qualifier `EQUIP`, while the
> 15-minute record had all but 4.75 h); NLDI basin (no split catchment); PRISM over the gauge basin from
> the national grids in `extracted/` (changed from the cube on 31 Oct), filling up to 1% of edge cells
> with the basin mean; baseflow bracketed by a constant line and
> a straight start-to-end line instead of a Lyne–Hollick filter.
> Regulation (1 Nov 2026): effective storage, each dam's capacity capped by the storm over its NID drainage
> area; Animas at Durango, Mancos and McElmo moved from regulated to minor.
>
> First results (PRISM days Oct 20–27, flow to Oct 31): Pagosa Springs 09342500, 727 km², 182 mm,
> peak 9,230 cfs, runoff ratio 0.33–0.40; Animas at Durango 09361500, 1,765 km², 127 mm, peak
> 11,200 cfs, ratio 0.26–0.30. Basin areas agree with USGS within 0.1% (Pagosa) and 3% (Animas: NLDI
> polygon smaller than the published drainage area).
>
> **Update 31 Oct 2026:** `--all-gauges` runs every USGS stream gauge in the study basin, and each gauge
> gets a regulation class from upstream National Inventory of Dams storage expressed as a depth over its
> basin (unregulated < 1 mm, minor < 10 mm, regulated ≥ 10 mm), plus storage as a share of the storm's
> precipitation. Adds a `dams` layer, `runoff_summary.csv`, cached per-gauge results so runs resume after
> the USGS limit (1,000 requests/hour), and optional `USGS_API_KEY`.
>
> **Follow-ups:**
> - Reservoir accounting: add Navajo Reservoir (and other large reservoirs') storage change from
>   Reclamation data to downstream flow, so main-stem gauges become a usable PRISM check.
> - Distinguish off-channel reservoirs and transbasin diversions (San Juan–Chama Project, Dolores to McElmo)
>   in the regulation class.
> - Main-stem gauges with multi-day flood gaps (Bloomfield, Farmington, Shiprock) were skipped; revisit
>   when USGS publishes estimated or revised values, or allow a gap-filling method with explicit flags.
> - Hydrograph plus basin hyetograph plot (PNG), from `runoff_<site>_flow.csv` and the daily precipitation.
> - Lyne–Hollick filter as a third baseflow estimate; tune for 15-minute data.
> - ~~Rain-only ratio once section 4 lands~~ (done 1 Nov 2026). Outputs are `runoff_<site>.json` and
>   `runoff_summary.csv`, not the `validation/runoff.json` planned below.
> - Hand-check one runoff volume against USGS daily means on days with complete records (the done criterion).
> - Rerun with `--after-days` 7+ once the recession data exists (flow was cut off at the time of the run).

**Goal:** Compare precipitation volume with streamflow volume at the basin outlet (or a chosen gauge).

**Why:** The standard hydrologic sanity check. Runoff ratio = runoff volume ÷ precipitation volume.
Values above 1 (or far above typical values for the basin) point to an underestimate of precipitation,
snowmelt contribution, or a timing problem.

**Data (verify):** USGS daily discharge. The legacy NWIS Water Services API
(`waterservices.usgs.gov`) is being replaced by the Water Data APIs (`api.waterdata.usgs.gov`);
target the supported one. Example gauges (verify IDs): San Juan River at Pagosa Springs (09342500),
Animas River at Durango (09361500).

**Approach:**
1. Gauge chosen by `--gauge <site id>`; the basin for comparison is that gauge's upstream drainage.
   Either its HUC or a delineated drainage polygon (USGS NLDI or StreamStats can return one; verify).
2. Baseflow separation (for example a Lyne–Hollick digital filter, or a straight-line method)
   to isolate event runoff.
3. Integrate event runoff over a window extended past the rain (for example +7 days) to catch the recession.
4. Report runoff volume, precipitation volume, and runoff ratio, using **rain only** where
   the rain/snow split (section 4) is available.

**Outputs:** a hydrograph plot with the rainfall chart, `validation/runoff.json`.

**Done when:** a run against a real gauge reproduces a hand-checked runoff volume within a few percent.

### 1.3 Multi-product comparison — L
**Goal:** Run the same window and basin through other precipitation products and report the spread.

**Why:** Disagreement between products is the most defensible uncertainty bound on a storm total,
especially in mountains where gauges are sparse and radar is blocked.

**Candidates (verify access and day definitions):**
| Product | Notes |
|---|---|
| Stage IV (NCEP) | Radar plus gauges, hourly/6-hourly/24-hourly, ~4 km, HRAP grid; good timing, weak in complex terrain |
| MRMS | Radar-based, high resolution, recent years |
| AORC (NOAA) | Hourly ~1 km analysis; public on AWS; check how current it is |
| gridMET | Daily ~4 km; days end at a fixed local time; differs from PRISM's 12:00 UTC |
| Daymet | Daily 1 km; lags by months |

**Approach:** add a product adapter layer (download → common grid or native grid → daily totals with
explicit day boundaries), reusing `cell_area_m2` for volume on each product's own grid. Report the
basin volume per product and the spread (min/max, coefficient of variation).

**Done when:** a comparison table for one storm across at least two products, with day boundaries
aligned and documented.

---

## 2. Automated QC checks — S/M

> **Status: built 1 Nov 2026** as `scripts/qc.py`, called by `prism_watershed.py` and `storm_total.py`;
> results print at the end of each run and go into `run_manifest.json`. A failure exits with status 1 after
> outputs are written. Changes from the plan: a nodata gap that is the same every day (outside PRISM
> coverage) is a warning, not a failure, so border basins don't always fail; cells missing on only some
> days follow the warn >0 / fail >1% rule. A corrupt cached zip is renamed `.bad`. Station density
> threshold defaults to 0.5 per 1,000 km² (San Juan: 1.80); revisit after more basins.

**Goal:** Every run ends with a QC report of pass / warn / fail checks, also saved as JSON.

| Check | Rule (starting point) | Level |
|---|---|---|
| Completeness | Every day in the window downloaded and in the cube | fail |
| Download integrity | Response is a zip; zip opens; SHA-256 recorded; a PRISM error text is reported verbatim | fail |
| Grid consistency | Same transform, shape and CRS for every day | fail |
| Nodata inside basin | Share of basin cells with nodata on any day > 0 | warn (>0), fail (>1%) |
| Physical limits | No negative values; daily max above a threshold (for example 400 mm) flagged | fail / warn |
| Totals reconcile | Cube sum = event-total raster; volume raster sum = reported volume | fail |
| Release mix | Window includes release 1–2 days, or mixes releases | warn |
| Station density | Stations in `stn.csv` inside basin + buffer, per 1,000 km² | warn below a threshold |
| Boundary agreement | Grid basin area vs polygon area differ by > 1% | warn |

**Approach:** a `qc.py` module of pure functions (no network) called by both scripts; results
go into the run manifest (section 5). Flags: `--qc-strict` turns warnings into failures.

**Done when:** each check has a unit test with a passing and a failing synthetic case, and a QC block
prints at the end of each run.

---

## 3. Uncertainty and error propagation

### 3.1 Revision sensitivity — S (built 1 Nov 2026)

> **Built:** `release` variable in the cube and tags on clipped tifs; `storm_total.py` lists releases;
> `prism_watershed.py --refresh` (`--target-release`, `--refresh-age`) re-downloads, archives superseded
> zips in `raw_zips/releases/`, re-extracts stale `extracted/` folders, prints per-day changes and keeps
> `<var>_<res>_release_history.csv`. `validate_runoff.py` recomputes saved gauges' precipitation when
> releases change. `storm_total.py` reports the window volume change from each day's first recorded
> release to its latest (`revision_change`), which is the revision term for 3.3.

**Goal:** Show how much totals move as PRISM revises the grids (8 releases over ~6 months).

**Approach:**
- Store each day's release number in the cube (`release` variable on the time axis) and print the
  releases in every window report.
- Optional `--refresh` re-pulls days whose release is below a target. It must respect the
  2-downloads-per-24-hours limit: keep the old zip (rename by release) instead of deleting it.
- A `compare_releases` step reports volume change between releases for the same window.

**Done when:** a window report lists releases, and re-running after a refresh shows the volume change.

### 3.2 Boundary sensitivity — S (built 1 Nov 2026)

> **Built:** `storm_total.py` reports the window volume by cell center, area-weighted (cell shares from
> shapely intersections, no `exactextract`) and any cell touched, from the national grids in `extracted/`
> (`boundary_volumes`). San Juan Oct 20–27 (release 2): 4.6846 / 4.6852 / 4.7634 km³, range 1.68%.

**Goal:** Report how much the basin definition changes the total.

**Approach:** compute volume three ways from the same grid:
1. cell center inside the polygon (current)
2. any cell touching the polygon (`all_touched=True`)
3. area-weighted by the fraction of each cell inside the polygon (for example with `exactextract`,
   not currently installed)

Report the range. For the San Juan (HUC 1408) the grid area differs from the polygon area by 0.02%;
small basins will differ far more.

**Done when:** the window report includes the three volumes and their spread.

### 3.3 Combined uncertainty statement — M (after 1.1, 1.3, 3.1, 3.2)

> **Status: built 1 Nov 2026** (without 1.3) as `scripts/uncertainty.py`, reported by `storm_total.py`,
> saved to `ppt_<res>_uncertainty_<start>_<end>.json`, shown on the QGIS basin label; method in
> `docs/UNCERTAINTY.md`. Decisions: bounding range = sum of component lows and highs; gauge term spans
> all-gauge and independent-gauge implied volumes; boundary term uses area-weighted vs centers ("touched"
> shown only); revision term = observed change so far, both directions, flagged while releases < 8, no
> allowance for future revisions; rain/snow is a separate rain-volume range, not part of the total.
> San Juan Oct 20–27: 4.685 km³, 4.625–4.916 (−1.3% to +4.9%). Add product spread when 1.3 lands.

**Goal:** Report volume as a value with a range, not a bare number like 4.719 km³.

**Approach:** treat sources separately and document the combination:
- station-based error (1.1) scaled to the basin, with a note that it is in-sample
- product spread (1.3)
- revision change (3.1)
- boundary range (3.2)

Start with a transparent bounding approach (report each component and an overall range) before any
statistical combination. Spatially correlated errors mean per-cell errors cannot simply be added in quadrature.

**Done when:** `storm_total.py` prints `Volume: X km³ (range A–B)` with a breakdown, and the method is
written up in `docs/`.

---

## 4. Rain/snow partition — M

> **Status: built 1 Nov 2026.** `prism_watershed.py --var tmean` shares the precipitation folder (own cube
> and summary CSV, no event total); `storm_total.py` splits rain and snow per cell and day with a linear
> ramp (`--all-snow-c` 0, `--all-rain-c` 3), reports both, writes `--rain-tif`, `--snow-tif`,
> `--rain-snow-nc`, and QC checks rain + snow = precipitation; `validate_runoff.py` adds a rain-only
> ratio and snow share per gauge. San Juan Oct 20–27: 3.5% snow basin-wide (release 2); 64–76% at the high Animas
> gauges, whose rain-only ratios reach 1.06–1.17. SNOTEL check built as `validate_snow.py` (NRCS AWDB):
> over 23 stations PRISM's snow is 1.16× the observed SWE gain with 0–3 °C (0.9× with −1–3 °C; ramps ending
> at 2 °C give ≤0.7×); near Silverton the split is ~2× observed. Keep 0–3 °C until more storms are checked.
> QGIS project: rain/snow rasters (recognized by name) and a styled `snotel` layer, theme "Rain and snow".

**Goal:** Split each cell's daily precipitation into rain and snow-water.

**Why:** `ppt` counts all precipitation. At high elevations (for example the San Juan Mountains above
~3,000 m in late October), part of a storm falls as snow and does not run off right away. Without the
split, the runoff ratio (1.2) is misleading.

**Approach:**
- Download PRISM `tmean` (and optionally `tmin`/`tmax`) for the same days; the download script already
  supports `--var`. The 2-downloads-per-24-hours limit applies per file, so plan the extra downloads.
- Per cell and day: snow fraction from temperature, either a single threshold (for example 1 °C) or a
  linear ramp between two thresholds (for example 0 °C to 3 °C). Make the thresholds parameters.
- Optional check: compare with SNOTEL snow water equivalent changes at stations in the basin.

**Outputs:** `rain_mm` and `snow_mm` variables in the cube; rain and snow volumes in the window report;
rain/snow maps.

**Done when:** rain + snow equals total precipitation exactly, and the report gives rain and snow volumes.

---

## 5. Provenance and reproducibility

### 5.1 Run manifest — S

> **Status: built 1 Nov 2026** as `scripts/manifest.py`. `<out>/run_manifest.json` holds a `runs` list with
> one entry appended per run (folders are refreshed over time, so entries are never overwritten). The
> validation and subbasin scripts don't write entries yet. Tool version is the git commit until there
> are tagged releases.

**Goal:** A `run_manifest.json` in each output folder recording how the outputs were made.

**Contents:** command line and parsed arguments; start and end time; tool version and git commit;
Python and key package versions (rasterio, GDAL, PROJ, xarray, netCDF4); basin source (HUC and WBD
query URL, or boundary file path and checksum); per day: PRISM URL, zip SHA-256, release number,
`info.txt` fields; output files and checksums; the QC report (section 2).

**Done when:** every run writes the manifest, and a test checks its required keys.

### 5.2 CF-compliant NetCDF — S
> **Status: built 1 Nov 2026.** Cube, volume and rain/snow NetCDFs carry `Conventions = "CF-1.10"`,
> `title`, `institution`, `source`, `history`, `references`; `time_bnds` (12:00 UTC to 12:00 UTC) with the
> time value kept at 00:00 UTC on the label date, which is the day's midpoint (moving it to 12:00 UTC, as
> planned below, would put it on a bound and break date selection); standard names, `cell_methods`,
> `cell_measures`, GRS80 on the grid mapping. Checked with netCDF4/xarray and by opening in QGIS 3.44 as
> raster and mesh; no formal CF checker run (cfchecker needs UDUNITS, not installed).

**Goal:** Make the cube and volume NetCDFs follow CF conventions.

**Changes:**
- time at 12:00 UTC on each PRISM day label, with `time_bnds` covering 12:00 UTC the previous day to
  12:00 UTC that day (also fixes the 12-hour label offset in the QGIS Temporal Controller)
- `cell_methods = "time: sum"` on precipitation and volume variables
- `Conventions = "CF-1.10"`, plus `title`, `institution`, `source`, `history`, `references`
- `release` per time step (from 3.1)

**Done when:** the files pass a CF checker (for example `cfchecker` or the IOOS compliance checker)
and still open in QGIS and xarray.

### 5.3 Version control — S
> **Status: started 1 Nov 2026.** Git repository with history; the manifest records the commit (with
> `-dirty`). Not yet: tagged releases and a remote.

`git init`, a first commit, tagged releases, and the commit recorded in the manifest. The
`.gitignore` already excludes data and rasters.

---

## 6. Storm context

### 6.1 Return period (precipitation frequency) — M
**Goal:** State how rare the storm was, for example "the 48-hour basin-average depth is about the
25-year value".

**Data (verify):** NOAA Atlas 14 Precipitation Frequency Data Server (PFDS) point estimates and
gridded frequency estimates; NOAA Atlas 15 is replacing Atlas 14, so check which is current
for the region.

**Approach:** compute the largest 1-, 2- and 3-day totals per cell (rolling sums over the cube); compare
with gridded frequency estimates for the same durations; report the return period at the max cell and
for the basin average. Note the mismatch between PRISM days (fixed 12:00 UTC) and Atlas durations
(any 24-hour period).

### 6.2 Percent of normal — S
**Goal:** Compare the storm with PRISM 30-year normals (1991–2020), for example "the event equals X% of
normal October precipitation".

**Data (verify):** PRISM normals through the same web service; check the current URL form for normals.

### 6.3 Rainfall timing (hyetograph) — S
Daily basin-average bars and a running total, from the cube. Hourly timing needs a product like
Stage IV or AORC (1.3).

### 6.4 Subbasin breakdown — S/M
> **Status: built 31 Oct 2026** as `scripts/subbasin_totals.py` (`totals.gpkg`: `basin_total`, `subbasins`),
> mapped by `build_qgis_project.py` (depth fill, volume labels, Volume by subbasin theme, basin total label).
> Follow-ups: optional 3D extrusion of subbasin volume; per-day subbasin volumes for the report.

Depth and volume per HUC8 or HUC10 within the basin (WBD query by prefix, for example `huc8 LIKE '1408%'`),
as a table and map.

---

## 7. Reporting — M

> **Status: built 1 Nov 2026** as `scripts/report.py`: self-contained HTML (no external resources, light
> and dark, hover values, a data table per chart) plus SVG figures. Sections: headline tiles (volume with
> range, mean/max, releases, snow share, QC), storm timeline (precipitation per PRISM day with rain/snow
> above discharge and shaded event runoff at up to three gauges), PRISM vs gauges, runoff ratios, uncertainty
> components, SNOTEL snow check. Not yet: maps in the report (use the QGIS project), PDF, a QC table, method
> notes beyond the captions.

**Goal:** One command produces a storm report (HTML, optionally PDF) with:
- summary: window, basin, area, volume with range, mean and max depth
- maps: total depth, volume per cell, rain/snow split
- hyetograph and, where available, the hydrograph with runoff ratio
- QC table, release numbers, validation statistics
- method notes and data citations (PRISM, USGS WBD, others used)

Built from the manifest and outputs, so a report can be regenerated without re-running downloads.

---

## 7a. Organized QGIS project (opt-in) — M/L

> **Status: phase 1 built 31 Oct 2026** as `scripts/build_qgis_project.py`, run separately after the tools.
> It finds outputs by standard names instead of a layer catalog (no tool changes needed), and adds
> data-driven precipitation and station-difference classes. Not yet: the layer catalog, `--qgis`/
> `--qgis-project`/`--qlr` options on each tool, incremental updates that keep user edits (it rebuilds and
> keeps a `.bak`), the daily NetCDF as a mesh layer, print layouts, 3D view, atlas. Projects for the
> three test basins are in each test folder's `QGIS/` (built with QGIS 3.44.10). Also built: map themes,
> a basin bookmark, Temporal Controller settings, rain/snow and SNOTEL layers, the uncertainty range on the
> basin label, and a test that builds a real project. The project is written to `<out>/QGIS/<name>.qgz`,
> not `<out>/<basin>.qgz` as planned below.

**Goal:** Any tool can build or update a logically organized, styled QGIS project from its outputs,
when asked. Without the option, tools behave as now.

**Why:** Outputs are spread across rasters, NetCDFs, GeoPackages and CSVs; arranging and styling them
by hand each run is slow and inconsistent.

**Design:**
- **Layer catalog (always written):** each tool writes or updates `<out>/layers.json` with every output
  it produced: path, layer name, role, group, style file, sort order, default visibility, units, date
  window and provenance (PRISM release numbers, run time). Overlaps with the run manifest (5.1).
  No QGIS dependency.
- **Builder:** `scripts/build_qgis_project.py --out DIR` reads the catalog and creates or updates
  `<out>/<basin>.qgz`. Runs under QGIS's own Python (headless PyQGIS); on macOS the app's wrapper
  `/Applications/QGIS-<version>.app/Contents/MacOS/python` works (checked with QGIS 4.2.3; unset
  `PROJ_DATA` and Anaconda's PDAL plugin path first to avoid noisy errors).
- **Options on each tool (all off by default):**
  | Option | Effect |
  |---|---|
  | `--qgis` | After the run, build or update `<out>/<basin>.qgz` |
  | `--qgis-project PATH` | Add or update the layers in that project instead (new or existing) |
  | `--qlr` | Write a QGIS layer definition file to drag into any open project |
  | `--qgis-python PATH` (or `QGIS_PYTHON`) | Which QGIS Python to use; auto-detected otherwise |
  If QGIS isn't found, `--qgis` prints the builder command instead of failing.
- **Updating safely:** layers the builder manages carry a custom property and are matched by source,
  so reruns update rather than duplicate. Layers the user added and styles the user edited are left
  alone (`--restyle` resets styles). Don't overwrite a project that is open in QGIS (QGIS doesn't lock
  project files); write a copy or say so.

**Layout** (draw order points → lines → polygons → rasters → basemap):
```
<Basin> (HUC <code>) — PRISM days <start>–<end>
├─ Validation: USGS gauges (by regulation) · Gauge basins · NID dams ·
│              Rain gauges PRISM − gauge (storm) · Rain gauges daily (Temporal)
├─ Storm totals: depth (mm) · volume per cell (m³) · Windows (one subgroup per storm_total.py window)
├─ Daily precipitation (mutually exclusive): daily stack (mesh, Temporal) · one raster per day
├─ Boundaries: study basin
└─ Basemap: OpenStreetMap
```

**Styling and settings:**
- One shared ramp and fixed class breaks for all precipitation-depth layers (daily and window totals),
  so a color means the same depth everywhere; a separate ramp for volume. Colorblind-safe palettes.
- Readable layer names with units and dates; provenance in each layer's metadata.
- Relative paths, project saved in the data folder (portable); projected CRS chosen automatically
  (NAD83 UTM zone at the basin centroid) for 3D and measurement; map themes ("Storm totals",
  "Validation", "Daily animation"); a basin bookmark; Temporal Controller preset to the window, 1-day step.

**Phases:**
1. Catalog in each tool; builder makes a new `.qgz` with groups, styles, names, relative paths, CRS.
2. Incremental updates, `--qgis`/`--qgis-project`/`--qlr` options, map themes, bookmarks, temporal setup.
3. Print layout template, preconfigured 3D view, atlas with one page per gauge basin.

**QGIS version:** decided 31 Oct 2026: build with QGIS 3.x (3.44 LTR); QGIS 4 opens those projects.

**Done when:** running the full workflow with `--qgis` produces a project that opens with every output
grouped, named, styled and ordered as above; rerunning updates it without duplicates; tests run the
builder headless when PyQGIS is available and are skipped otherwise.

---

## 8. Engineering

| Item | Notes | Effort |
|---|---|---|
| Packaging | `pyproject.toml`; commands `prism-watershed`, `storm-total`, `find-huc`; scripts become a package | S |
| Config files | YAML run configs (basin, window, outputs) for repeat runs; CLI flags override | S |
| Logging | `logging` with levels and a log file per run, in place of `print` | S |
| HTTP robustness | Retries with exponential backoff for network errors, never for the download-limit response; clear messages | S |
| Shared download cache | One national zip cache (for example `~/.cache/prism`) shared by all basins, so several basins over the same dates download each file once | S |
| Resume | Interrupted runs pick up where they stopped (cache already covers downloads; add processing state) | S |
| Batch runs | Many basins or windows from one config | M |
| CI | GitHub Actions running pytest on every push, in the conda environment | S |
| Type checking and lint | Type hints, `mypy`, `ruff` | S |
| Dependency pinning | A lock file (for example `conda-lock`) for exact reproducibility, alongside `environment.yml` | S |
| Performance | Window clipping (read only the basin window from each COG) instead of full-grid reads; parallel downloads within PRISM's limits | M |

---

## Open questions
- Which second precipitation product to support first (Stage IV for timing, AORC for resolution)?
- Report format: HTML only, or also PDF for distribution?
- ~~QGIS project builder (7a): which QGIS version to target?~~ Decided: build with QGIS 3.x (3.44 LTR here);
  QGIS 4.2.3 opens those projects, the reverse isn't guaranteed.
- Rain/snow thresholds: a fixed default, or calibrated against SNOTEL for the region?
