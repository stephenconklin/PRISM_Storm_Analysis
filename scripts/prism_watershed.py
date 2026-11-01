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

Requires: pip install requests numpy rasterio geopandas xarray netCDF4  (optional: truststore)
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
import numpy as np
import rasterio
from rasterio import features
from rasterio.mask import mask
import requests

from storm_total import RES_CODES, build_cube, cell_area_m2

try:
    # Use the OS trust store: services.nacse.org chains to emSign Root CA - G1,
    # which macOS trusts but certifi's bundle does not.
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

PRISM_URL = "https://services.nacse.org/prism/data/get/{region}/{res}/{var}/{date}"
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
            cache.write_bytes(r.content)
        gdf = gpd.read_file(cache)
        if gdf.empty:
            sys.exit(f"No WBD polygon found for HUC {huc}")
        print(f"Basin: {', '.join(gdf.get('name', gdf.iloc[:, 0]).astype(str))}")
    return gdf.dissolve()


def zip_name(var, res, region, day, fmt=""):
    """Cache file name for one daily zip, unique per variable, resolution, region, date and format.

    Matches PRISM's own name for the default COG (prism_ppt_us_30s_20261020.zip); other formats
    get a suffix so they never collide with it.
    """
    suffix = f"_{fmt}" if fmt else ""
    return f"prism_{var}_{region}_{RES_CODES[res]}_{day.strftime('%Y%m%d')}{suffix}.zip"


def fetch_zip(var, res, region, day, fmt, path, session):
    """Download one daily zip to path. Returns False (printing PRISM's message) if no zip came back."""
    ymd = day.strftime("%Y%m%d")
    url = PRISM_URL.format(region=region, res=res, var=var, date=ymd)
    params = {"format": fmt} if fmt else None
    r = session.get(url, params=params, timeout=300)
    ctype = r.headers.get("Content-Type", "")
    if r.status_code != 200 or "zip" not in ctype:
        # The service returns a short text message when data isn't available yet
        # or when the 2-downloads-per-24h limit is hit.
        print(f"  {ymd}: not downloaded ({r.status_code}) {r.text[:200].strip()}")
        return False
    path.write_bytes(r.content)
    return True


def download_day(var, res, region, day, fmt, raw_dir, session):
    """Download one daily zip (cached). Returns the zip path or None."""
    path = raw_dir / zip_name(var, res, region, day, fmt)
    if path.exists() or fetch_zip(var, res, region, day, fmt, path, session):
        return path
    return None


def parse_release(text):
    """PRISM_DATASET_RELEASE_NUMBER (1-8) from an info.txt, as a string ('' if absent)."""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "PRISM_DATASET_RELEASE_NUMBER":
            return value.strip()
    return ""


def zip_release(zpath):
    """Release number recorded in a PRISM zip's info.txt ('' if none)."""
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if "info" in Path(name).name and name.endswith(".txt"):
                return parse_release(z.read(name).decode(errors="ignore"))
    return ""


def folder_release(folder):
    """Release number in an extracted folder's info.txt ('' if none)."""
    for info in Path(folder).rglob("*info*.txt"):
        return parse_release(info.read_text(errors="ignore"))
    return ""


def due_for_refresh(release, last_downloaded, today, target, age_days):
    """True if a cached day is below the target release and was last downloaded age_days or more ago."""
    if release.isdigit() and int(release) >= target:
        return False
    return (today - last_downloaded).days >= age_days


def refresh_day(var, res, region, day, fmt, raw_dir, session):
    """Re-download a cached zip. Returns (old release, new release), or None if nothing came back.

    If the release changed, the old zip moves to raw_zips/releases/<name>_r<old>.zip; if not, the new
    download replaces it, so the file's date records when it was last checked.
    """
    path = raw_dir / zip_name(var, res, region, day, fmt)
    old = zip_release(path)
    part = path.with_name(path.name + ".part")
    if not fetch_zip(var, res, region, day, fmt, part, session):
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


def day_stats(arr, transform):
    """Basin mean, max (mm) and volume (m³) of one clipped day."""
    area = np.repeat(cell_area_m2(transform, arr.shape[0])[:, None], arr.shape[1], axis=1)
    volume = float((arr / 1000 * area).sum())
    return {"basin_mean_mm": round(float(arr.mean()), 2), "basin_max_mm": round(float(arr.max()), 2),
            "basin_volume_m3": round(volume)}


def clipped_day(path, summary_csv):
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
    return release, day_stats(arr, transform)


HISTORY_FIELDS = ["date", "release", "basin_mean_mm", "basin_max_mm", "basin_volume_m3", "recorded"]


def update_history(path, entries):
    """Add (date, release) rows not already in the release history CSV; returns all rows."""
    rows = []
    if path.exists():
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    seen = {(r["date"], r["release"]) for r in rows}
    for e in entries:
        if (e["date"], e["release"]) not in seen:
            rows.append({k: e[k] for k in HISTORY_FIELDS})
            seen.add((e["date"], e["release"]))
    rows.sort(key=lambda r: (r["date"], int(r["release"]) if r["release"].isdigit() else 0))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return rows


def prism_coverage(arr, transform, basin):
    """Share of the basin with no PRISM data: (missing %, missing km², basin km²).

    arr is a masked array on the clipped grid (masked = no data). Cells count by center, so
    the basin area here is the PRISM-cell area. PRISM covers the conterminous US only, so
    basins reaching into Canada or Mexico have a gap.
    """
    inside = features.geometry_mask(basin.to_crs(4269).geometry, arr.shape, transform, invert=True)
    area = np.repeat(cell_area_m2(transform, arr.shape[0])[:, None], arr.shape[1], axis=1)
    basin_m2 = float(area[inside].sum())
    missing_m2 = float(area[inside & np.ma.getmaskarray(arr)].sum())
    return (missing_m2 / basin_m2 * 100 if basin_m2 else 0.0), missing_m2 / 1e6, basin_m2 / 1e6


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD, first PRISM day")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD, last PRISM day (capped at yesterday)")
    basin_src = ap.add_mutually_exclusive_group(required=True)
    basin_src.add_argument("--huc", help="WBD hydrologic unit code, 2-16 digits (see find_huc.py)")
    basin_src.add_argument("--boundary", help="your own boundary polygon file instead of WBD")
    ap.add_argument("--var", default="ppt")
    ap.add_argument("--res", default="800m", choices=["800m", "4km"])
    ap.add_argument("--region", default="us")
    ap.add_argument("--format", default="", help="blank = COG GeoTIFF (default); or nc, bil, asc")
    ap.add_argument("--out", help="output folder (default data/huc<code> or data/<boundary name>)")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between requests")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download cached days below --target-release to pick up PRISM's revisions")
    ap.add_argument("--target-release", type=int, default=8, help="release to refresh up to (default 8, final)")
    ap.add_argument("--refresh-age", type=float, default=7,
                    help="only re-download zips last downloaded at least this many days ago (default 7)")
    a = ap.parse_args()
    if a.huc and not (a.huc.isdigit() and len(a.huc) % 2 == 0 and 2 <= len(a.huc) <= 16):
        ap.error("--huc must be 2, 4, 6, 8, 10, 12, 14 or 16 digits")

    start = dt.date.fromisoformat(a.start)
    end = min(dt.date.fromisoformat(a.end), dt.date.today() - dt.timedelta(days=1))
    out = Path(a.out or (f"data/huc{a.huc}" if a.huc else f"data/{Path(a.boundary).stem}"))
    raw, ext, clipped = out / "raw_zips", out / "extracted", out / "clipped"
    for d in (raw, ext, clipped):
        d.mkdir(parents=True, exist_ok=True)

    basin = get_boundary(a.huc, a.boundary, out)
    session = requests.Session()
    session.headers["User-Agent"] = "prism-data-wrangler"

    summary_csv = out / "daily_basin_summary.csv"
    today, recorded = dt.date.today(), dt.datetime.now().isoformat(timespec="seconds")
    rows, history, changes, total, meta = [], [], [], None, None
    for day in daterange(start, end):
        ymd = day.strftime("%Y%m%d")
        cached = raw / zip_name(a.var, a.res, a.region, day, a.format)
        clip_path = clipped / f"{a.var}_{a.res}_{ymd}_clip.tif"
        before = clipped_day(clip_path, summary_csv)  # what this folder held before this run
        if before:
            history.append({"date": day.isoformat(), "release": before[0], **before[1], "recorded": recorded})
        if (a.refresh and cached.exists() and due_for_refresh(
                zip_release(cached), dt.date.fromtimestamp(cached.stat().st_mtime), today,
                a.target_release, a.refresh_age)):
            change = refresh_day(a.var, a.res, a.region, day, a.format, raw, session)
            time.sleep(a.pause)
            if change:
                print(f"  {ymd}: " + (f"release {change[0] or '?'} -> {change[1]}" if change[0] != change[1]
                                      else f"still release {change[1]}"))
        had_zip = cached.exists()
        zpath = download_day(a.var, a.res, a.region, day, a.format, raw, session)
        if not had_zip:
            time.sleep(a.pause)
        if zpath is None:
            continue
        grid, release = extract_grid(zpath, ext)
        arr, meta = clip(grid, basin, clip_path, release)
        total = arr.copy() if total is None else total + arr
        stats = day_stats(arr, meta["transform"])
        rows.append({"date": day.isoformat(), "release": release,
                     "basin_mean_mm": stats["basin_mean_mm"], "basin_max_mm": stats["basin_max_mm"],
                     "basin_mean_in": round(float(arr.mean()) / 25.4, 3)})
        history.append({"date": day.isoformat(), "release": release, **stats, "recorded": recorded})
        if before and before[0] != release:
            changes.append((day, before, release, stats))
        print(f"  {ymd}: mean {rows[-1]['basin_mean_mm']} mm, max {rows[-1]['basin_max_mm']} mm "
              f"[release {release or 'n/a'}]")

    if not rows:
        sys.exit("No data downloaded.")

    history_csv = out / f"{a.var}_{a.res}_release_history.csv"
    update_history(history_csv, history)
    if changes:
        print("\nRevised by PRISM since the last run (basin mean / volume):")
        for day, (old_rel, old), new_rel, new in changes:
            dv = new["basin_volume_m3"] - old["basin_volume_m3"]
            pct = f" ({dv / old['basin_volume_m3'] * 100:+.1f}%)" if old["basin_volume_m3"] else ""
            print(f"  {day}: release {old_rel or '?'} -> {new_rel}: {old['basin_mean_mm']} -> "
                  f"{new['basin_mean_mm']} mm, {old['basin_volume_m3']:.4g} -> {new['basin_volume_m3']:.4g} m³{pct}")
        print(f"All releases seen per day: {history_csv.name}")
    if any(r["release"].isdigit() and int(r["release"]) < 8 for r in rows):
        print("Some days are not final (release 8); PRISM revises them for about 6 months. "
              "Re-run later with --refresh.")

    with open(summary_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    tag = f"{rows[0]['date']}_{rows[-1]['date']}".replace("-", "")
    meta.update(dtype="float32")
    with rasterio.open(out / f"{a.var}_{a.res}_total_{tag}.tif", "w", **meta) as dst:
        dst.write(total.filled(-9999).astype("float32"), 1)
    print(f"\nEvent total: basin mean {total.mean():.1f} mm ({total.mean()/25.4:.2f} in), "
          f"max {total.max():.1f} mm ({total.max()/25.4:.2f} in)")
    missing_pct, missing_km2, basin_km2 = prism_coverage(total, meta["transform"], basin)
    if missing_pct > 0.5:
        print(f"WARNING: {missing_pct:.1f}% of the basin ({missing_km2:,.0f} of {basin_km2:,.0f} km²) has no PRISM "
              "data, usually because it lies outside the conterminous US. Totals and volumes cover only the rest.")
    # The cube holds every clipped day in this --out folder, not just this run's window.
    cube = build_cube(clipped, a.var, a.res, out / f"{a.var}_{a.res}_daily_clip.nc",
                      attrs={"basin_area_km2": round(basin_km2, 1),
                             "basin_without_prism_pct": round(missing_pct, 2)})
    print(f"Daily cube: {cube.name} (use storm_total.py for any date range)")
    print(f"Outputs in {out.resolve()}")


if __name__ == "__main__":
    main()
