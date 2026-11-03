#!/usr/bin/env python3
"""
Storm context: the storm compared with PRISM's 1991-2020 precipitation normals.

For a window of PRISM days it reports, over the basin (area-weighted, the cells with PRISM data):
    window normal   normal precipitation for those days: each month's normal prorated by the share of
                    the month's days in the window (or the sum of daily normals with --daily-normals)
    percent         storm total as a percentage of the window normal
    month           storm total as a percentage of the normal total for each month the window touches

Normals are not on PRISM's data web service; they come from PRISM's file server
(https://data.prism.oregonstate.edu/normals/<region>/<res>/<var>/<monthly|daily>/, checked 1 Nov 2026),
cached in <out>/normals_zips/ (a monthly 800 m normal is ~58 MB, a daily one ~67 MB; 4 km files are far
smaller). Writes <out>/ppt_<res>_normals_<start>_<end>.json, read by report.py, and with --pct-tif a
GeoTIFF of percent of normal per cell.

Usage examples:
    python storm_context.py --out data/huc1408 --start 2026-10-20 --end 2026-10-27
    python storm_context.py --out data/huc1408 --start 2026-10-20 --end 2026-10-27 --pct-tif data/huc1408/pct_of_normal.tif
"""
import argparse
import calendar
import datetime as dt
import json
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import from_bounds
import requests
import xarray as xr

import manifest
from storm_total import RES_CODES, cube_transform, window_total, write_tif

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

NORMALS_URL = "https://data.prism.oregonstate.edu/normals/{region}/{res}/{var}/{step}/{name}"


def normal_name(var, res, region, month, day=None):
    """PRISM's file name for a 1991-2020 normal: prism_ppt_us_30s_202010_avg_30y.zip (monthly) or
    prism_ppt_us_30s_20201022_avg_30y.zip (daily; the year 2020 only labels the period)."""
    stamp = f"2020{month:02d}" + (f"{day:02d}" if day else "")
    return f"prism_{var}_{region}_{RES_CODES[res]}_{stamp}_avg_30y.zip"


def window_weights(start, end, daily=False):
    """[(month, day or None, weight)]: the normals to add up for the PRISM days start..end.

    Monthly: each month's normal times the share of that month's days in the window. Daily: each day's
    normal with weight 1 (Feb 29 uses its own normal, which PRISM provides).
    """
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    if daily:
        return [(d.month, d.day, 1.0) for d in days]
    counts = {}
    for d in days:
        counts[(d.year, d.month)] = counts.get((d.year, d.month), 0) + 1
    return [(m, None, n / calendar.monthrange(y, m)[1]) for (y, m), n in counts.items()]


def fetch(url, path, session, pause=2.0):
    """Download url to path unless it is cached. Returns path; exits with PRISM's message on failure."""
    if path.exists():
        return path
    print(f"  downloading {path.name} ...", flush=True)
    r = session.get(url, timeout=600)
    if r.status_code != 200 or "zip" not in r.headers.get("Content-Type", ""):
        sys.exit(f"Could not download {url} ({r.status_code}): {r.text[:200].strip()}")
    part = path.with_name(path.name + ".part")
    part.write_bytes(r.content)
    part.replace(path)
    time.sleep(pause)
    return path


def normal_on_cube(zpath, ds):
    """A normal grid (mm) read on the cube's cells. Exits if PRISM's grid doesn't line up with the cube."""
    with zipfile.ZipFile(zpath) as z:
        tif = next(n for n in z.namelist() if n.endswith(".tif"))
        dest = zpath.parent.parent / "normals" / zpath.stem
        if not (dest / tif).exists():
            z.extract(tif, dest)
    t = cube_transform(ds)
    h, w = ds.sizes["lat"], ds.sizes["lon"]
    with rasterio.open(dest / tif) as src:
        win = from_bounds(t.c, t.f + h * t.e, t.c + w * t.a, t.f, src.transform).round_offsets().round_lengths()
        arr = src.read(1, window=win, masked=True, boundless=True).astype("float64").filled(np.nan)
        if arr.shape != (h, w) or abs(src.window_transform(win).c - t.c) > abs(t.a) / 100:
            sys.exit(f"{tif} is not on the cube's grid; normals must match the cube's resolution.")
    return arr


def percent_of_normal(storm_mm, normal_mm, area_m2):
    """Area-weighted basin depths and percent: (storm mm, normal mm, percent or None) over cells with both."""
    ok = ~np.isnan(storm_mm) & ~np.isnan(normal_mm)
    a = np.where(ok, area_m2, 0.0)
    if not a.sum():
        return None, None, None
    s = float((np.where(ok, storm_mm, 0) * a).sum() / a.sum())
    n = float((np.where(ok, normal_mm, 0) * a).sum() / a.sum())
    return s, n, (s / n * 100 if n else None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="folder prism_watershed.py wrote to")
    ap.add_argument("--start", help="first PRISM day (default: first day in the cube)")
    ap.add_argument("--end", help="last PRISM day (default: last day in the cube)")
    ap.add_argument("--res", default="800m", choices=["800m", "4km"])
    ap.add_argument("--region", default="us", help="PRISM region (default us)")
    ap.add_argument("--daily-normals", action="store_true",
                    help="sum daily normals for the window instead of prorating monthly ones (larger downloads)")
    ap.add_argument("--pct-tif", help="also write percent of normal per cell to this GeoTIFF")
    a = ap.parse_args()
    run = manifest.start_run("storm_context.py", a)

    out = Path(a.out)
    nc = out / f"ppt_{a.res}_daily_clip.nc"
    if not nc.exists():
        sys.exit(f"No cube at {nc}. Run prism_watershed.py with --out {a.out} first.")
    zdir = out / "normals_zips"
    zdir.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "prism-storm-analysis"

    with xr.open_dataset(nc) as ds:
        days = ds.time.values.astype("datetime64[D]")
        start = dt.date.fromisoformat(a.start or str(days.min()))
        end = dt.date.fromisoformat(a.end or str(days.max()))
        r = window_total(ds, str(start), str(end))
        storm = r["total_mm"].values
        area = ds.cell_area.values

        window_normal = np.zeros_like(storm)
        months = {}
        for month, day, weight in window_weights(start, end, a.daily_normals):
            name = normal_name("ppt", a.res, a.region, month, day)
            url = NORMALS_URL.format(region=a.region, res=a.res, var="ppt", step="daily" if day else "monthly",
                                     name=name)
            grid = normal_on_cube(fetch(url, zdir / name, session), ds)
            window_normal = window_normal + weight * grid
            if not a.daily_normals:
                months[month] = grid
        if a.daily_normals:  # monthly totals are still needed for the month comparison
            for month in sorted({d.month for d in (start, end)}):
                name = normal_name("ppt", a.res, a.region, month)
                url = NORMALS_URL.format(region=a.region, res=a.res, var="ppt", step="monthly", name=name)
                months[month] = normal_on_cube(fetch(url, zdir / name, session), ds)

        s_mm, n_mm, pct = percent_of_normal(storm, window_normal, area)
        month_rows = []
        for month, grid in sorted(months.items()):
            _, m_mm, m_pct = percent_of_normal(storm, grid, area)
            month_rows.append({"month": calendar.month_name[month], "normal_mm": round(m_mm, 1),
                               "storm_pct_of_month": None if m_pct is None else round(m_pct, 1)})
        if a.pct_tif:
            with np.errstate(divide="ignore", invalid="ignore"):
                pct_grid = np.where(window_normal > 0, storm / window_normal * 100, np.nan)
            write_tif(r["total_mm"].copy(data=pct_grid), ds.crs.attrs["crs_wkt"], a.pct_tif, "percent")

    method = "sum of daily normals" if a.daily_normals else "monthly normals prorated by days in the window"
    result = {"window": {"start": str(start), "end": str(end)}, "normals": "PRISM 1991-2020", "method": method,
              "storm_mm": round(s_mm, 1), "window_normal_mm": round(n_mm, 1),
              "percent_of_normal": None if pct is None else round(pct, 1), "months": month_rows}
    span = f"{start:%Y%m%d}_{end:%Y%m%d}"
    jpath = out / f"ppt_{a.res}_normals_{span}.json"
    jpath.write_text(json.dumps(result, indent=2))
    print(f"PRISM days {start} to {end}: basin {s_mm:.1f} mm vs a normal of {n_mm:.1f} mm for these days "
          f"({method}): {pct:.0f}% of normal")
    for m in month_rows:
        print(f"  = {m['storm_pct_of_month']:.0f}% of the normal {m['month']} total ({m['normal_mm']:.0f} mm)")
    if a.pct_tif:
        print(f"Percent of normal per cell: {a.pct_tif}")
    run.update(window=result["window"], results=result,
               outputs=manifest.files_entry([jpath] + ([a.pct_tif] if a.pct_tif else []), out))
    manifest.append_run(out, run)


if __name__ == "__main__":
    main()
