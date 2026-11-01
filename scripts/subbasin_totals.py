#!/usr/bin/env python3
"""
Storm precipitation depth and water volume for the whole basin and for each subbasin.

Reads the daily cube in --out and writes <out>/totals.gpkg with two layers:
    basin_total  the study basin with its storm totals (area, mean and max depth, volume)
    subbasins    each USGS Watershed Boundary Dataset unit inside the basin at one HUC level,
                 with depth, volume, share of the basin's volume, and PRISM coverage
plus subbasin_totals.csv. Both layers carry default QGIS styles.

Subbasin level: --digits, or by default the first HUC level below the study basin with at
least 20 units (for example HUC10 inside a HUC4, HUC12 inside a HUC8). With --boundary (no
HUC code), WBD units intersecting the boundary are used and kept when at least half of each
lies inside it.

Totals use the same cells and ellipsoidal cell areas as storm_total.py: cells count when
their centers fall inside a unit, and cells without PRISM data (for example in Canada) are
left out and reported as coverage, so subbasin volumes add up to the basin total.

Usage examples:
    python subbasin_totals.py --out data/huc1408
    python subbasin_totals.py --out data/huc1408 --digits 12 --start 2026-10-22 --end 2026-10-23
"""
import argparse
import csv
import re
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import xarray as xr

import manifest
from storm_total import M3_PER_ACRE_FT, zone_totals
from validate_stations import load_basin, write_layer


def pick_digits(huc, count, min_units=20):
    """First HUC level below `huc` with at least min_units units (count(digits) -> int), up to 12."""
    digits = len(huc) + 2
    while digits < 12 and count(digits) < min_units:
        digits += 2
    return min(digits, 12)


def subbasin_rows(units, totals, basin_volume_m3, digits):
    """Rows for the subbasins layer, ranked by volume (1 = most water)."""
    rows = []
    for u, t in zip(units, totals):
        p = u["properties"]
        rows.append({"huc": p.get(f"huc{digits}"), "level": f"HUC{digits}", "name": p.get("name"),
                     "states": p.get("states"), "area_km2": round(t["area_km2"], 2),
                     "coverage_pct": round(t["coverage_pct"], 1),
                     "mean_mm": None if np.isnan(t["mean_mm"]) else round(t["mean_mm"], 1),
                     "max_mm": None if np.isnan(t["max_mm"]) else round(t["max_mm"], 1),
                     "volume_m3": round(t["volume_m3"]),
                     "volume_acre_ft": round(t["volume_m3"] / M3_PER_ACRE_FT),
                     "share_pct": round(t["volume_m3"] / basin_volume_m3 * 100, 2) if basin_volume_m3 else None})
    for rank, r in enumerate(sorted(rows, key=lambda r: -r["volume_m3"]), 1):
        r["rank"] = rank
    return rows


def main():
    from observations import wbd_count, wbd_units

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="folder prism_watershed.py wrote to")
    ap.add_argument("--start", help="first PRISM day (default: first day in the cube)")
    ap.add_argument("--end", help="last PRISM day (default: last day in the cube)")
    ap.add_argument("--digits", type=int, choices=[4, 6, 8, 10, 12, 14, 16], help="subbasin HUC level")
    ap.add_argument("--boundary", help="study basin polygon (default: the wbd_huc*.geojson in --out)")
    ap.add_argument("--res", default="800m", choices=["800m", "4km"])
    a = ap.parse_args()
    run = manifest.start_run("subbasin_totals.py", a)

    out = Path(a.out)
    nc = out / f"ppt_{a.res}_daily_clip.nc"
    if not nc.exists():
        sys.exit(f"No cube at {nc}. Run prism_watershed.py with --out {a.out} first.")
    basin = load_basin(out, a.boundary)
    wbd = sorted(out.glob("wbd_huc*.geojson"))
    huc = re.search(r"wbd_huc(\d+)", wbd[0].name).group(1) if wbd and not a.boundary else None
    name = "Study basin"
    if wbd and not a.boundary:
        names = gpd.read_file(wbd[0]).get("name")
        name = str(names.iloc[0]) if names is not None and len(names) else name

    bbox = tuple(basin.total_bounds)
    if huc:
        if not a.digits and len(huc) >= 12:
            sys.exit(f"HUC {huc} has {len(huc)} digits; subbasins need --digits 14 or 16 (mapped only in places).")
        digits = a.digits or pick_digits(huc, lambda d: wbd_count(d, prefix=huc))
        if digits <= len(huc):
            sys.exit(f"--digits must be longer than the study basin's HUC ({len(huc)} digits).")
        units = wbd_units(digits, prefix=huc)
    else:
        digits = a.digits or 12
        units = wbd_units(digits, bbox=bbox)
        poly = basin.geometry.iloc[0]
        units = [u for u in units if gpd.GeoSeries.from_features([u], crs=4269).iloc[0].intersection(poly).area
                 >= 0.5 * gpd.GeoSeries.from_features([u], crs=4269).iloc[0].area]
    if not units:
        sys.exit(f"No HUC{digits} units found in the study basin.")
    ugdf = gpd.GeoDataFrame.from_features(units, crs=4269)

    with xr.open_dataset(nc) as ds:
        days = ds.time.values.astype("datetime64[D]")
        start, end = a.start or str(days.min()), a.end or str(days.max())
        basin_t = zone_totals(ds, [basin.geometry.iloc[0]], start, end)[0]
        unit_t = zone_totals(ds, list(ugdf.geometry), start, end)

    vol = basin_t["volume_m3"]
    total_row = {"name": name, "huc": huc, "prism_start": start, "prism_end": end,
                 "area_km2": round(basin_t["area_km2"], 1), "basin_cells_km2": round(basin_t["cells_km2"], 1),
                 "coverage_pct": round(basin_t["coverage_pct"], 1), "mean_mm": round(basin_t["mean_mm"], 1),
                 "max_mm": round(basin_t["max_mm"], 1), "volume_m3": round(vol), "volume_km3": round(vol / 1e9, 4),
                 "volume_acre_ft": round(vol / M3_PER_ACRE_FT), "subbasin_level": f"HUC{digits}", "res": a.res,
                 "subbasins": len(units)}
    rows = subbasin_rows(units, unit_t, vol, digits)
    for r in rows:
        r.update(prism_start=start, prism_end=end)

    gpkg = out / "totals.gpkg"
    write_layer(gpd.GeoDataFrame([total_row], geometry=[basin.geometry.iloc[0]], crs=4269), gpkg, "basin_total")
    write_layer(gpd.GeoDataFrame(rows, geometry=list(ugdf.geometry), crs=4269), gpkg, "subbasins")
    with open(out / "subbasin_totals.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["rank"]))

    summed = sum(r["volume_m3"] for r in rows)
    print(f"{name}" + (f" (HUC {huc})" if huc else "") + f", PRISM days {start} to {end}")
    print(f"Basin: {basin_t['area_km2']:,.0f} km² with PRISM data ({basin_t['coverage_pct']:.1f}% coverage), "
          f"mean {basin_t['mean_mm']:.1f} mm, max {basin_t['max_mm']:.1f} mm, "
          f"{vol / 1e9:.3f} km³ = {vol / M3_PER_ACRE_FT:,.0f} acre-ft")
    print(f"{len(rows)} HUC{digits} subbasins" + (f"; their volumes add up to {summed / vol * 100:.1f}% of the basin total"
                                                   if vol else "; no precipitation in the window"))
    print("Largest by volume:")
    for r in sorted(rows, key=lambda r: r["rank"])[:10]:
        print(f"  {r['rank']:>3}. {r['name'][:40]:<40} {r['area_km2']:>8,.0f} km²  {r['mean_mm'] or 0:6.1f} mm  "
              f"{r['volume_acre_ft']:>10,} acre-ft  " + (f"{r['share_pct']:5.1f}%" if r["share_pct"] is not None else "    -"))
    low = [r for r in rows if r["coverage_pct"] < 99]
    if low:
        print(f"{len(low)} subbasins are partly without PRISM data (coverage < 99%), "
              "usually outside the conterminous US or at the basin edge.")
    print(f"Outputs: {gpkg}, {out / 'subbasin_totals.csv'}")
    run.update(window={"start": str(start), "end": str(end)},
               outputs=manifest.files_entry([out / "totals.gpkg", out / "subbasin_totals.csv"], out))
    manifest.append_run(out, run)


if __name__ == "__main__":
    main()
