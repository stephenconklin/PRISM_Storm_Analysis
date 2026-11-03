#!/usr/bin/env python3
"""
Download PRISM daily grids (precipitation by default, 800 m) for any date range
and clip them to a watershed boundary.

Dates are PRISM days: each ends at 12:00 UTC on its date, so pad a storm window
by a day on each side.

PRISM web service (post Oct-2025 syntax):
    https://services.nacse.org/prism/data/get/<region>/<res>/<var>/<YYYYMMDD>[?format=nc|bil|asc]
    region: us | res: 800m or 4km | var: ppt, tmin, tmax, tmean, tdmean, vpdmin, vpdmax
    Default format is Cloud Optimized GeoTIFF inside a .zip.

PRISM rule: the same file can only be downloaded twice per 24 h per IP, so this
script caches every zip and never re-requests one it already has, unless --refresh asks.

PRISM revises each daily grid up to 8 times over ~6 months (release 1 at D+1, 2 at D+5,
then about monthly; 8 is final). --refresh re-downloads cached days below --target-release
that were last downloaded at least --refresh-age days ago. A superseded zip is kept in
raw_zips/releases/<name>_r<release>.zip, and <var>_<res>_release_history.csv keeps each
day's basin mean and volume for every release seen, so the folder can be refreshed over time.

The basin boundary comes from the USGS Watershed Boundary Dataset (WBD) REST
service via --huc (find codes with find_huc.py), or from your own file via
--boundary. Output goes to data/huc<code> or data/<boundary name> unless --out
is given; use one --out folder per basin.

Usage examples:
    python prism_watershed.py --huc 14080104 --start 2026-10-20 --end 2026-10-27
    python prism_watershed.py --huc 1408 --start 2026-10-20 --end 2026-10-27 --out /data/my_basin
    python prism_watershed.py --boundary my_basin.gpkg --start 2026-10-20 --end 2026-10-27
    python prism_watershed.py --huc 1408 --start 2026-10-20 --end 2026-10-27 --out /data/my_basin --refresh

Also writes <out>/<var>_<res>_daily_clip.nc, a NetCDF stack of every clipped day
in --out; storm_total.py reads it to total depth and volume for any date range.

Each run ends with QC checks (qc.py; --qc-strict turns warnings into failures, and any
failure gives exit status 1) and appends a record of the run, with per-day zip checksums,
releases and info.txt fields, to <out>/run_manifest.json.

Requires the packages in environment.yml / requirements.txt (truststore is needed to reach PRISM).
"""
import argparse
import csv
import datetime as dt
import shutil
import sys
import time
import zipfile
from pathlib import Path

import geopandas as gpd
import netCDF4
import numpy as np
import pandas as pd
import rasterio
from rasterio import features
from rasterio.mask import mask
import requests
import shapely
import xarray as xr

import manifest
import qc
from storm_total import RES_CODES, VAR_UNITS, area_grid, build_cube, geodesic_km2

try:
    # Use the OS trust store: services.nacse.org chains to emSign Root CA - G1,
    # which macOS trusts but certifi's bundle does not.
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

PRISM_URL = "https://services.nacse.org/prism/data/get/{region}/{res}/{var}/{date}"
# Release numbers per day without downloading (checked 1 Nov 2026): one request covers a date range;
# rows are [data date, release date, element, grid count (= release 1-8), data URL].
RELEASE_URL = "https://services.nacse.org/prism/data/get/releaseDate/{region}/{res}/{var}/{start}/{end}"
WBD_URL = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/{layer}/query"


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def get_boundary(huc, boundary_file, out_dir):
    """Return a GeoDataFrame for the basin (dissolved to one polygon)."""
    if boundary_file:
        gdf = gpd.read_file(boundary_file)
    else:
        cache = out_dir / f"wbd_huc{huc}.geojson"
        if not cache.exists():
            layer = len(huc) // 2          # WBD layers: 1=HUC2, 2=HUC4, 3=HUC6, 4=HUC8, 5=HUC10, 6=HUC12
            field = f"huc{len(huc)}"
            params = {"where": f"{field}='{huc}'", "outFields": f"{field},name",
                      "returnGeometry": "true", "outSR": "4269", "f": "geojson"}
            print(f"Fetching WBD boundary for HUC {huc} ...")
            r = requests.get(WBD_URL.format(layer=layer), params=params, timeout=120)
            r.raise_for_status()
            # ArcGIS reports server errors as HTTP 200 with {"error": ...}; don't cache those.
            if "features" not in r.json():
                sys.exit(f"WBD service error for HUC {huc}: {r.text[:300]}")
            cache.write_bytes(r.content)
        gdf = gpd.read_file(cache)
        if gdf.empty:
            sys.exit(f"No WBD polygon found for HUC {huc}")
        print(f"Basin: {', '.join(gdf.get('name', gdf.iloc[:, 0]).astype(str))}")
    basin = gdf.dissolve()
    if boundary_file:  # keep a copy, so later steps and the QGIS project find the basin in the folder
        basin.to_crs(4269).to_file(out_dir / "boundary.geojson", driver="GeoJSON")
    return basin


def zip_name(var, res, region, day, fmt=""):
    """Cache file name for one daily zip, unique per variable, resolution, region, date and format.

    Matches PRISM's own name for the default COG (prism_ppt_us_30s_20261020.zip); other formats
    get a suffix so they never collide with it.
    """
    suffix = f"_{fmt}" if fmt else ""
    return f"prism_{var}_{region}_{RES_CODES[res]}_{day.strftime('%Y%m%d')}{suffix}.zip"


def fetch_zip(var, res, region, day, fmt, path, session, errors=None):
    """Download one daily zip to path. Returns False (printing PRISM's message, and saving it in
    errors[day] if given) if no zip came back."""
    ymd = day.strftime("%Y%m%d")
    url = PRISM_URL.format(region=region, res=res, var=var, date=ymd)
    params = {"format": fmt} if fmt else None
    try:
        r = session.get(url, params=params, timeout=300)
    except requests.RequestException as e:
        message = f"request failed ({e.__class__.__name__})"
        print(f"  {ymd}: {message}")
        if errors is not None:
            errors[day] = message
        return False
    ctype = r.headers.get("Content-Type", "")
    if r.status_code != 200 or "zip" not in ctype:
        # The service returns a short text message when data isn't available yet
        # or when the 2-downloads-per-24h limit is hit.
        message = f"not downloaded ({r.status_code}) {r.text[:200].strip()}"
        print(f"  {ymd}: {message}")
        if errors is not None:
            errors[day] = message
        return False
    path.write_bytes(r.content)
    return True


def download_day(var, res, region, day, fmt, raw_dir, session, errors=None):
    """Download one daily zip (cached). Returns the zip path or None."""
    path = raw_dir / zip_name(var, res, region, day, fmt)
    if path.exists() or fetch_zip(var, res, region, day, fmt, path, session, errors):
        return path
    return None


def parse_release(text):
    """PRISM_DATASET_RELEASE_NUMBER (1-8) from an info.txt, as a string ('' if absent)."""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "PRISM_DATASET_RELEASE_NUMBER":
            return value.strip()
    return ""


def zip_info(zpath):
    """Text of a PRISM zip's info.txt ('' if none)."""
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if "info" in Path(name).name and name.endswith(".txt"):
                return z.read(name).decode(errors="ignore")
    return ""


def zip_release(zpath):
    """Release number recorded in a PRISM zip's info.txt ('' if none)."""
    return parse_release(zip_info(zpath))


def zip_problem(zpath):
    """'' if the zip opens and its members' CRCs check out, else what is wrong."""
    try:
        with zipfile.ZipFile(zpath) as z:
            bad = z.testzip()
        return f"corrupt member {bad}" if bad else ""
    except zipfile.BadZipFile as e:
        return f"not a valid zip ({e})"


def folder_release(folder):
    """Release number in an extracted folder's info.txt ('' if none)."""
    for info in Path(folder).rglob("*info*.txt"):
        return parse_release(info.read_text(errors="ignore"))
    return ""


def available_releases(var, res, region, start, end, session):
    """{date: release number} PRISM currently has for each day, from the releaseDate service (no grids
    downloaded), or None if the service can't be reached."""
    url = RELEASE_URL.format(region=region, res=res, var=var, start=start.strftime("%Y%m%d"),
                             end=end.strftime("%Y%m%d"))
    try:
        r = session.get(url, params={"json": "true"}, timeout=60)
        r.raise_for_status()
        return parse_release_rows(r.json())
    except (requests.RequestException, ValueError):
        return None


def parse_release_rows(rows):
    """{date: release} from releaseDate rows [data date, release date, element, grid count, URL]."""
    out = {}
    for row in rows or []:
        try:
            out[dt.date.fromisoformat(row[0])] = int(row[3])
        except (IndexError, TypeError, ValueError):
            continue
    return out


def due_for_refresh(release, last_downloaded, today, target, age_days, available=None):
    """True if a cached day should be downloaded again.

    With available (the release PRISM has now, from the releaseDate service): only when it is newer than
    the cached release, up to target. Without it: when the cached day is below target and was last
    downloaded age_days or more ago.
    """
    if release.isdigit() and int(release) >= target:
        return False
    if available is not None:
        return available > (int(release) if release.isdigit() else 0)
    return (today - last_downloaded).days >= age_days


def refresh_day(var, res, region, day, fmt, raw_dir, session, errors=None):
    """Re-download a cached zip. Returns (old release, new release), or None if nothing came back.

    If the release changed, the old zip moves to raw_zips/releases/<name>_r<old>.zip; if not, the new
    download replaces it, so the file's date records when it was last checked.
    """
    path = raw_dir / zip_name(var, res, region, day, fmt)
    old = zip_release(path)
    part = path.with_name(path.name + ".part")
    if not fetch_zip(var, res, region, day, fmt, part, session, errors):
        return None
    problem = zip_problem(part)
    if problem:
        part.unlink()
        message = f"refreshed download is {problem}; kept the cached zip"
        print(f"  {day.strftime('%Y%m%d')}: {message}")
        if errors is not None:
            errors[day] = message
        return None
    new = zip_release(part)
    if new != old:
        archive = raw_dir / "releases" / f"{path.stem}_r{old or 'unknown'}.zip"
        archive.parent.mkdir(exist_ok=True)
        path.replace(archive)
    part.replace(path)
    return old, new


def extract_grid(zpath, extract_dir):
    """Unzip and return (raster path, release number string or '')."""
    # PRISM releases each daily grid 8 times over ~6 months (D+1, D+5, then ~monthly);
    # info.txt records which one this is as PRISM_DATASET_RELEASE_NUMBER (1-8).
    release = zip_release(zpath)
    dest = extract_dir / zpath.stem
    if dest.exists() and folder_release(dest) != release:
        shutil.rmtree(dest)  # unzipped from an earlier release of this day
    if not dest.exists():
        with zipfile.ZipFile(zpath) as z:
            z.extractall(dest)
    grids = [p for p in dest.rglob("*") if p.suffix.lower() in (".tif", ".bil", ".nc", ".asc")]
    if not grids:
        raise FileNotFoundError(f"No raster in {zpath}")
    return grids[0], release


def clip(grid, basin, out_path, release=""):
    """Clip a raster to the basin polygon, tagging the PRISM release; returns the masked array (mm)."""
    with rasterio.open(grid) as src:
        shapes = basin.to_crs(src.crs).geometry
        data, transform = mask(src, shapes, crop=True, filled=True, nodata=-9999)
        meta = src.meta.copy()
    meta.update(driver="GTiff", height=data.shape[1], width=data.shape[2],
                transform=transform, nodata=-9999, compress="deflate")
    with rasterio.open(out_path, "w", **meta) as dst:
        dst.write(data)
        dst.update_tags(PRISM_DATASET_RELEASE_NUMBER=release)
    arr = np.ma.masked_equal(data[0], -9999)
    return arr, meta


def area_mean(arr, transform):
    """Mean of a masked lat/lon array weighted by ellipsoidal cell area (cells shrink northward)."""
    area = area_grid(transform, arr.shape)
    return float(np.ma.average(arr, weights=area))


def day_stats(arr, transform, var="ppt"):
    """Area-weighted basin mean, max (mm) and volume (m³) of one clipped precipitation day; for other
    variables, area-weighted basin mean, min and max in the variable's units."""
    mean = area_mean(arr, transform)
    if var != "ppt":
        return {"basin_mean": round(mean, 2), "basin_min": round(float(arr.min()), 2),
                "basin_max": round(float(arr.max()), 2)}
    area = area_grid(transform, arr.shape)
    volume = float((arr / 1000 * area).sum())
    return {"basin_mean_mm": round(mean, 2), "basin_max_mm": round(float(arr.max()), 2),
            "basin_volume_m3": round(volume)}


def summary_name(var, res):
    """daily_basin_summary.csv for 800 m precipitation (its original name); <var>_<res>_daily_basin_summary.csv
    for anything else, so a folder can hold several variables and resolutions."""
    return "daily_basin_summary.csv" if (var, res) == ("ppt", "800m") else f"{var}_{res}_daily_basin_summary.csv"


def clipped_day(path, summary_csv, var="ppt"):
    """(release, stats) of an existing clipped tif, or None. Tifs clipped before releases were tagged
    take the release from daily_basin_summary.csv."""
    if not path.exists():
        return None
    with rasterio.open(path) as src:
        arr, transform = src.read(1, masked=True), src.transform
        release = src.tags().get("PRISM_DATASET_RELEASE_NUMBER", "")
    if not release and summary_csv.exists():
        ymd = path.name.split("_")[-2]
        with open(summary_csv, newline="") as f:
            for r in csv.DictReader(f):
                if r["date"].replace("-", "") == ymd:
                    release = r.get("release", "")
    return release, day_stats(arr, transform, var)


def stale_totals(out, var, res, revised, current):
    """Event-total rasters in out (other than current) whose window includes a revised day."""
    stale = []
    for tif in sorted(Path(out).glob(f"{var}_{res}_total_*.tif")):
        span = tif.stem.rsplit("_", 2)[-2:]
        if tif.name == current or len(span) != 2 or not all(x.isdigit() for x in span):
            continue
        t0, t1 = (dt.datetime.strptime(x, "%Y%m%d").date() for x in span)
        if any(t0 <= d <= t1 for d in revised):
            stale.append(tif.name)
    return stale


def stations_near(stn_csv, zone):
    """PRISM input stations (from a zip's stn.csv) inside zone, a lon/lat polygon."""
    df = pd.read_csv(stn_csv, skiprows=1)  # first line describes the file
    return int(shapely.contains_xy(zone, df["Longitude"].to_numpy(), df["Latitude"].to_numpy()).sum())


def update_history(path, entries):
    """Add (date, release) rows not already in the release history CSV; returns all rows."""
    fields = list(entries[0])
    rows = []
    if path.exists():
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    seen = {(r["date"], r["release"]) for r in rows}
    for e in entries:
        if (e["date"], e["release"]) not in seen:
            rows.append({k: e[k] for k in fields})
            seen.add((e["date"], e["release"]))
    rows.sort(key=lambda r: (r["date"], int(r["release"]) if r["release"].isdigit() else 0))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return rows


def prism_coverage(arr, transform, basin, grid_bounds=None):
    """Share of the basin with no PRISM data: (missing %, missing km², basin km²).

    arr is a masked array on the clipped grid (masked = no data). Cells count by center, so
    the basin area here is the PRISM-cell area. PRISM covers the conterminous US only, so
    basins reaching into Canada or Mexico have a gap. The clip stops at the edge of PRISM's
    grid, so the part of the basin beyond grid_bounds (the national grid's left, bottom, right,
    top) is added from the polygon's ellipsoidal area.
    """
    inside = features.geometry_mask(basin.to_crs(4269).geometry, arr.shape, transform, invert=True)
    area = area_grid(transform, arr.shape)
    basin_m2 = float(area[inside].sum())
    missing_m2 = float(area[inside & np.ma.getmaskarray(arr)].sum())
    if grid_bounds is not None:
        beyond = basin.to_crs(4269).union_all().difference(shapely.box(*grid_bounds))
        beyond_m2 = geodesic_km2(beyond) * 1e6 if not beyond.is_empty else 0.0
        basin_m2 += beyond_m2
        missing_m2 += beyond_m2
    return (missing_m2 / basin_m2 * 100 if basin_m2 else 0.0), missing_m2 / 1e6, basin_m2 / 1e6


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD, first PRISM day")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD, last PRISM day (capped at yesterday)")
    basin_src = ap.add_mutually_exclusive_group(required=True)
    basin_src.add_argument("--huc", help="WBD hydrologic unit code, 2-16 digits (see find_huc.py)")
    basin_src.add_argument("--boundary", help="your own boundary polygon file instead of WBD")
    ap.add_argument("--var", default="ppt", help="PRISM variable: ppt (default), tmin, tmax, tmean, tdmean, vpdmin, vpdmax")
    ap.add_argument("--res", default="800m", choices=["800m", "4km"], help="grid resolution (default 800m)")
    ap.add_argument("--region", default="us", help="PRISM region (default us, the conterminous US)")
    ap.add_argument("--format", default="", help="blank = COG GeoTIFF (default); or nc, bil, asc")
    ap.add_argument("--out", help="output folder (default data/huc<code> or data/<boundary name>)")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between requests")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download cached days that PRISM has a newer release of (up to --target-release)")
    ap.add_argument("--target-release", type=int, default=8, help="release to refresh up to (default 8, final)")
    ap.add_argument("--refresh-age", type=float, default=7,
                    help="when PRISM's release service can't be reached: only re-download zips last downloaded "
                         "at least this many days ago (default 7)")
    ap.add_argument("--qc-strict", action="store_true", help="treat QC warnings as failures")
    ap.add_argument("--qc-max-daily-mm", type=float, default=400.0,
                    help="flag daily precipitation in any cell above this (default 400 mm)")
    ap.add_argument("--qc-station-buffer-km", type=float, default=10.0,
                    help="count PRISM input stations this far outside the basin (default 10 km)")
    ap.add_argument("--qc-min-stations", type=float, default=0.5,
                    help="warn below this many PRISM input stations per 1,000 km² (default 0.5)")
    a = ap.parse_args()
    run = manifest.start_run("prism_watershed.py", a)
    if a.huc and not (a.huc.isdigit() and len(a.huc) % 2 == 0 and 2 <= len(a.huc) <= 16):
        ap.error("--huc must be 2, 4, 6, 8, 10, 12, 14 or 16 digits")

    start = dt.date.fromisoformat(a.start)
    end = min(dt.date.fromisoformat(a.end), dt.date.today() - dt.timedelta(days=1))
    out = Path(a.out or (f"data/huc{a.huc}" if a.huc else f"data/{Path(a.boundary).stem}"))
    raw, ext, clipped = out / "raw_zips", out / "extracted", out / "clipped"
    for d in (raw, ext, clipped):
        d.mkdir(parents=True, exist_ok=True)

    basin = get_boundary(a.huc, a.boundary, out)
    run["basin"] = ({"huc": a.huc, "wbd_layer_url": WBD_URL.format(layer=len(a.huc) // 2),
                     "where": f"huc{len(a.huc)}='{a.huc}'",
                     "cached_file": manifest.files_entry(sorted(out.glob(f"wbd_huc{a.huc}.geojson")), out)}
                    if a.huc else {"boundary": str(Path(a.boundary).resolve()),
                                   "sha256": manifest.sha256(a.boundary)})
    utm = basin.estimate_utm_crs()
    station_zone = basin.to_crs(utm).buffer(a.qc_station_buffer_km * 1000).to_crs(4269).union_all()
    session = requests.Session()
    session.headers["User-Agent"] = "prism-storm-analysis"
    available = available_releases(a.var, a.res, a.region, start, end, session)

    summary_csv = out / summary_name(a.var, a.res)
    ppt = a.var == "ppt"
    today, recorded = dt.date.today(), dt.datetime.now().isoformat(timespec="seconds")
    rows, history, changes, total, meta = [], [], [], None, None
    errors, downloads, grids, stations, day_min, day_max, missing_m2, day_m3 = {}, [], [], {}, {}, {}, {}, []
    inside = area = always_missing = grid_bounds = None
    newer_available = []  # cached days that are due for a refresh
    requested = list(daterange(start, end))
    for day in requested:
        ymd = day.strftime("%Y%m%d")
        cached = raw / zip_name(a.var, a.res, a.region, day, a.format)
        clip_path = clipped / f"{a.var}_{a.res}_{ymd}_clip.tif"
        before = clipped_day(clip_path, summary_csv, a.var)  # what this folder held before this run
        if before:
            history.append({"date": day.isoformat(), "release": before[0], **before[1], "recorded": recorded})
        if cached.exists() and not zip_problem(cached) and due_for_refresh(
                zip_release(cached), dt.date.fromtimestamp(cached.stat().st_mtime), today,
                a.target_release, a.refresh_age, None if available is None else available.get(day, 0)):
            newer_available.append(day)
        if a.refresh and day in newer_available:
            change = refresh_day(a.var, a.res, a.region, day, a.format, raw, session, errors)
            time.sleep(a.pause)
            if change:
                print(f"  {ymd}: " + (f"release {change[0] or '?'} -> {change[1]}" if change[0] != change[1]
                                      else f"still release {change[1]}"))
        had_zip = cached.exists()
        zpath = download_day(a.var, a.res, a.region, day, a.format, raw, session, errors)
        if not had_zip:
            time.sleep(a.pause)
        if zpath is None:
            downloads.append({"date": day, "error": errors.get(day, "no zip")})
            continue
        problem = zip_problem(zpath)
        if problem:
            bad = zpath.with_name(zpath.name + ".bad")
            zpath.replace(bad)  # so the next run downloads it again
            errors[day] = f"{problem}; moved to {bad.name}"
            print(f"  {ymd}: {errors[day]}")
            downloads.append({"date": day, "error": errors[day]})
            continue
        grid, release = extract_grid(zpath, ext)
        try:
            arr, meta = clip(grid, basin, clip_path, release)
        except ValueError as e:  # rasterio: "Input shapes do not overlap raster."
            sys.exit(f"The basin does not overlap PRISM's {a.region} grid ({e}).")
        if grid_bounds is None:
            with rasterio.open(grid) as src:
                grid_bounds = tuple(src.bounds)
        if arr.count() == 0:
            sys.exit(f"No PRISM data inside the basin on {day}: it covers no PRISM cell center with data "
                     "(a basin smaller than one cell, or outside PRISM coverage).")
        total = arr.copy() if total is None else total + arr
        downloads.append({"date": day, "error": "", "refresh_error": errors.get(day, ""), "zip": zpath.name, "bytes": zpath.stat().st_size,
                          "sha256": manifest.sha256(zpath), "release": release,
                          "url": PRISM_URL.format(region=a.region, res=a.res, var=a.var, date=ymd)
                          + (f"?format={a.format}" if a.format else ""),
                          "info": manifest.info_fields(zip_info(zpath))})
        grids.append((day, meta["transform"], arr.shape, meta["crs"].to_wkt()))
        if inside is None:
            inside = features.geometry_mask(basin.to_crs(4269).geometry, arr.shape, meta["transform"], invert=True)
            area = area_grid(meta["transform"], arr.shape)
        if arr.shape == inside.shape:
            gone = np.ma.getmaskarray(arr) & inside
            always_missing = gone if always_missing is None else always_missing & gone
            missing_m2[day] = float(area[gone].sum())
        day_min[day], day_max[day] = float(arr.min()), float(arr.max())
        stn = sorted(Path(grid).parent.glob("*.stn.csv"))
        if stn:
            stations[day] = stations_near(stn[0], station_zone)
        stats = day_stats(arr, meta["transform"], a.var)
        if ppt:
            day_m3.append(stats["basin_volume_m3"])
            rows.append({"date": day.isoformat(), "release": release,
                         "basin_mean_mm": stats["basin_mean_mm"], "basin_max_mm": stats["basin_max_mm"],
                         "basin_mean_in": round(stats["basin_mean_mm"] / 25.4, 3)})
        else:
            rows.append({"date": day.isoformat(), "release": release, **stats, "units": VAR_UNITS.get(a.var, "")})
        history.append({"date": day.isoformat(), "release": release, **stats, "recorded": recorded})
        if before and before[0] != release:
            changes.append((day, before, release, stats))
        print(f"  {ymd}: " + (f"mean {stats['basin_mean_mm']} mm, max {stats['basin_max_mm']} mm" if ppt else
                              f"mean {stats['basin_mean']}, min {stats['basin_min']}, max {stats['basin_max']} "
                              f"{VAR_UNITS.get(a.var, '')}") + f" [release {release or 'n/a'}]")

    if not rows:
        sys.exit("No data downloaded.")

    history_csv = out / f"{a.var}_{a.res}_release_history.csv"
    update_history(history_csv, history)
    if changes:
        print("\nRevised by PRISM since the last run (basin mean" + (" / volume):" if ppt else "):"))
        for day, (old_rel, old), new_rel, new in changes:
            if not ppt:
                print(f"  {day}: release {old_rel or '?'} -> {new_rel}: {old['basin_mean']} -> {new['basin_mean']} "
                      f"{VAR_UNITS.get(a.var, '')}")
                continue
            dv = new["basin_volume_m3"] - old["basin_volume_m3"]
            pct = f" ({dv / old['basin_volume_m3'] * 100:+.1f}%)" if old["basin_volume_m3"] else ""
            print(f"  {day}: release {old_rel or '?'} -> {new_rel}: {old['basin_mean_mm']} -> "
                  f"{new['basin_mean_mm']} mm, {old['basin_volume_m3']:.4g} -> {new['basin_volume_m3']:.4g} m³{pct}")
        print(f"All releases seen per day: {history_csv.name}")
    if not a.refresh and available is not None and newer_available:
        print(f"PRISM has a newer release for {len(newer_available)} cached day(s) ("
              + ", ".join(f"{d:%b} {d.day} r{available[d]}" for d in newer_available) + "): rerun with --refresh.")
    elif any(r["release"].isdigit() and int(r["release"]) < 8 for r in rows):
        print("Some days are not final (release 8); PRISM revises them for about 6 months. "
              "Re-run later with --refresh.")

    # Like the cube, the summary covers every day in the folder: this run's days replace earlier rows.
    kept = []
    if summary_csv.exists():
        with open(summary_csv, newline="") as f:
            done = {r["date"] for r in rows}
            kept = [r for r in csv.DictReader(f) if r["date"] not in done]
    with open(summary_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(sorted(kept + rows, key=lambda r: r["date"]))

    tag = f"{rows[0]['date']}_{rows[-1]['date']}".replace("-", "")
    total_tif = out / f"{a.var}_{a.res}_total_{tag}.tif"
    complete = len(rows) == len(requested)
    if ppt and complete:  # a sum only means something for precipitation, and only over every day
        meta.update(dtype="float32")
        with rasterio.open(total_tif, "w", **meta) as dst:
            dst.write(total.filled(-9999).astype("float32"), 1)
        mean = area_mean(total, meta["transform"])
        print(f"\nEvent total: basin mean {mean:.1f} mm ({mean / 25.4:.2f} in), "
              f"max {total.max():.1f} mm ({total.max()/25.4:.2f} in)")
    elif ppt:
        print(f"\nEvent total not written: {len(requested) - len(rows)} of {len(requested)} days missing.")
    # The cube holds every clipped day in this --out folder, not just this run's window. Its coverage note
    # comes from all of them: a cell is outside PRISM coverage only if it has no data on any day.
    cube = build_cube(clipped, a.var, a.res, out / f"{a.var}_{a.res}_daily_clip.nc")
    with xr.open_dataset(cube) as ds:
        has_data = ds[a.var].notnull().any("time").values
    missing_pct, missing_km2, basin_km2 = prism_coverage(np.ma.masked_array(np.zeros(has_data.shape), mask=~has_data),
                                                         meta["transform"], basin, grid_bounds)
    with netCDF4.Dataset(cube, "a") as nc:
        nc.setncatts({"basin_area_km2": round(basin_km2, 1), "basin_without_prism_pct": round(missing_pct, 2)})
    if missing_pct > 0.5:
        print(f"WARNING: {missing_pct:.1f}% of the basin ({missing_km2:,.0f} of {basin_km2:,.0f} km²) has no PRISM "
              "data, usually because it lies outside the conterminous US. Totals and volumes cover only the rest.")
    print(f"Daily cube: {cube.name} (use storm_total.py for any date range)")
    for name in stale_totals(out, a.var, a.res, [c[0] for c in changes], total_tif.name):
        span = name.rsplit("_", 2)[-2:]
        print(f"Out of date (covers a revised day): {name}; rerun with --start {span[0][:4]}-{span[0][4:6]}-"
              f"{span[0][6:]} --end {span[1][:4]}-{span[1][4:6]}-{span[1][6:]} to update it")
    print(f"Outputs in {out.resolve()}")

    # QC: this run's days, plus the cube they went into.
    run_days = [dt.date.fromisoformat(r["date"]) for r in rows]
    with xr.open_dataset(cube) as ds:
        cube_days = set(ds.time.values.astype("datetime64[D]").astype(object))
        if ppt:  # the cube, read back, against the volumes computed from each clip as it was made
            daily = ds[a.var].sel(time=[np.datetime64(d, "ns") for d in run_days])
            cube_m3 = float((daily / 1000 * ds.cell_area).sum())
            clip_m3 = float(sum(day_m3))
    basin_m2 = float(area[inside].sum())
    constant_m2 = float(area[always_missing].sum()) if always_missing is not None else 0.0
    checks = [
        qc.completeness(requested, [d for d in run_days if d in cube_days]),
        qc.download_integrity(downloads),
        qc.grid_consistency(grids),
        qc.nodata_inside({str(d): (m - constant_m2) / basin_m2 * 100 for d, m in missing_m2.items()},
                         constant_m2 / basin_m2 * 100),
        (qc.physical_limits(day_min, day_max, a.qc_max_daily_mm) if ppt
         else qc.result("physical_limits", "skip", f"limits are for ppt, not {a.var}")),
        qc.totals_reconcile([("daily clip volumes = cube volumes", clip_m3, cube_m3)] if ppt else []),
        qc.release_mix({r["date"]: r["release"] for r in rows}),
        qc.station_density(stations, basin_km2, a.qc_station_buffer_km, a.qc_min_stations),
        qc.boundary_agreement(basin_km2, geodesic_km2(basin.to_crs(4269).union_all())),
    ]
    if a.qc_strict:
        checks = qc.apply_strict(checks)
    print("\n" + "\n".join(qc.report(checks)))

    run.update(window={"start": str(start), "end": str(end)}, days=downloads, qc=checks,
               available_releases=None if available is None else {str(d): r for d, r in available.items()},
               outputs=manifest.files_entry([cube, total_tif, summary_csv, history_csv]
                                            + [clipped / f"{a.var}_{a.res}_{d.strftime('%Y%m%d')}_clip.tif"
                                               for d in run_days], out))
    print(f"Run recorded in {manifest.append_run(out, run).name}")
    if qc.worst_level(checks) == "fail":
        sys.exit("QC failed (see above).")


if __name__ == "__main__":
    main()
