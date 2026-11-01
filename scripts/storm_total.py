#!/usr/bin/env python3
"""
Total precipitation depth and water volume over a basin for any date range.

Reads the daily cube that prism_watershed.py writes next to its other
outputs (<out>/ppt_<res>_daily_clip.nc): one layer per PRISM day, clipped to
the basin, plus the area of every grid cell.

Cell areas are computed on the GRS80 ellipsoid (NAD83) for each row of the
native 1/120-degree grid, so volumes need no reprojection or resampling.

Dates are PRISM day labels: the grid for Oct 23 covers 12:00 UTC Oct 22 to
12:00 UTC Oct 23 (6 AM to 6 AM Mountain Daylight Time).

The report also lists each day's PRISM release and, when <out>/<var>_<res>_release_history.csv
holds earlier releases, how much the window volume has changed since the first release recorded
for each day. If the national grids in <out>/extracted and the basin boundary are present, it
also reports boundary sensitivity: the window volume counting cells by center (the cube's rule),
any cell touching the basin, and each cell weighted by its share inside the basin.

Usage examples:
    python storm_total.py --out data/huc14080104                     # every day in the cube
    python storm_total.py --out data/huc14080104 --start 2026-10-22 --end 2026-10-23
    python storm_total.py --out data/huc14080104 --tif depth_mm.tif
    python storm_total.py --out data/huc14080104 --volume-tif volume_m3.tif
    python storm_total.py --out data/huc14080104 --volume-nc volume_m3.nc

The volume raster holds m³ of water per cell; its sum is the basin volume. The
volume NetCDF holds the same per day (`volume`) plus the window total (`volume_total`).
"""
import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio import features
from rasterio.transform import from_origin
from rasterio.windows import from_bounds
import shapely
import xarray as xr

# PRISM file names carry a resolution code instead of the resolution: 30s = 800 m, 25m = 4 km.
RES_CODES = {"800m": "30s", "4km": "25m"}
GRS80_A = 6378137.0
GRS80_F = 1 / 298.257222101
M3_PER_ACRE_FT = 1233.48184
GRID_SUFFIXES = (".tif", ".bil", ".nc", ".asc")  # preference order


def cell_area_m2(transform, height):
    """Ellipsoidal area (m^2) of one cell in each row of a north-up lat/lon grid."""
    e2 = GRS80_F * (2 - GRS80_F)
    e = np.sqrt(e2)

    def q(lat):
        s = np.sin(np.radians(lat))
        return s / (1 - e2 * s * s) - np.log((1 - e * s) / (1 + e * s)) / (2 * e)

    top = transform.f + np.arange(height) * transform.e
    bottom = top + transform.e
    b2 = GRS80_A ** 2 * (1 - e2)
    return np.abs(np.radians(transform.a) * b2 / 2 * (q(top) - q(bottom)))


def prism_day_files(extracted, var, res, day):
    """(grid path, stn.csv path or None) for one PRISM day and resolution in <out>/extracted."""
    ymd = day.strftime("%Y%m%d")
    folders = sorted(Path(extracted).glob(f"*{var}*{RES_CODES[res]}*{ymd}*"))
    grids = [p for f in folders if f.is_dir() for p in f.iterdir() if p.suffix.lower() in GRID_SUFFIXES]
    if not grids:
        return None, None
    grid = min(grids, key=lambda p: GRID_SUFFIXES.index(p.suffix.lower()))
    stn = sorted(grid.parent.glob("*.stn.csv"))
    return grid, (stn[0] if stn else None)


def build_cube(clipped_dir, var, res, nc_path, attrs=None):
    """Stack every clipped daily grid into one NetCDF with a time axis and cell areas."""
    tifs = sorted(Path(clipped_dir).glob(f"{var}_{res}_*_clip.tif"))
    if not tifs:
        return None
    days, layers, releases, transform, crs = [], [], [], None, None
    for tif in tifs:
        ymd = tif.name[len(f"{var}_{res}_"):][:8]
        with rasterio.open(tif) as src:
            if transform is not None and (src.transform != transform or src.shape != layers[0].shape):
                sys.exit(f"{tif.name} is on a different grid than the other clipped days; "
                         "use a separate --out folder for each basin.")
            transform, crs = src.transform, src.crs
            layers.append(src.read(1, masked=True).astype("float32").filled(np.nan))
            rel = src.tags().get("PRISM_DATASET_RELEASE_NUMBER", "")
            releases.append(int(rel) if rel.isdigit() else 0)
        days.append(np.datetime64(dt.datetime.strptime(ymd, "%Y%m%d").date(), "ns"))

    h, w = layers[0].shape
    lat = transform.f + (np.arange(h) + 0.5) * transform.e
    lon = transform.c + (np.arange(w) + 0.5) * transform.a
    area = np.repeat(cell_area_m2(transform, h)[:, None], w, axis=1)
    ds = xr.Dataset(
        {
            var: (("time", "lat", "lon"), np.stack(layers),
                  {"units": "mm", "long_name": f"PRISM daily {var}", "grid_mapping": "crs"}),
            "cell_area": (("lat", "lon"), area,
                          {"units": "m2", "long_name": "cell area on the GRS80 ellipsoid",
                           "grid_mapping": "crs"}),
            "release": (("time",), np.array(releases, dtype="int8"),
                        {"long_name": "PRISM dataset release number (1-8, 8 final; 0 unknown)"}),
            "crs": ((), np.int32(0), {"grid_mapping_name": "latitude_longitude",
                                      "crs_wkt": crs.to_wkt(), "spatial_ref": crs.to_wkt()}),
        },
        coords={
            "time": ("time", days),
            "lat": ("lat", lat, {"units": "degrees_north", "standard_name": "latitude"}),
            "lon": ("lon", lon, {"units": "degrees_east", "standard_name": "longitude"}),
        },
        attrs={**(attrs or {}), "source": "PRISM Group, Oregon State University, https://prism.oregonstate.edu",
               "prism_day": "Each time step covers 12:00 UTC the previous day to 12:00 UTC its date."},
    )
    ds.to_netcdf(nc_path, encoding={var: {"zlib": True, "complevel": 4, "_FillValue": np.nan}})
    return nc_path


def window_total(ds, start, end, var="ppt"):
    """Sum daily depth over [start, end] (PRISM day labels) and convert to volume."""
    wanted = np.arange(np.datetime64(start, "D"), np.datetime64(end, "D") + np.timedelta64(1, "D"))
    have = set(ds.time.values.astype("datetime64[D]"))
    missing = [str(d) for d in wanted if d not in have]
    if missing:
        sys.exit(f"Cube has no data for {', '.join(missing)}. "
                 "Run prism_watershed.py for those dates first.")
    daily = ds[var].sel(time=slice(wanted[0], wanted[-1]))
    releases = ([int(v) for v in ds.release.sel(time=slice(wanted[0], wanted[-1])).values]
                if "release" in ds else [0] * len(wanted))
    total = daily.sum("time", skipna=False)
    inside = total.notnull()
    area = float(ds.cell_area.where(inside).sum())
    volume = float((total / 1000 * ds.cell_area).where(inside).sum())
    return {"daily_mm": daily, "total_mm": total, "volume_cell_m3": total / 1000 * ds.cell_area,
            "days": len(wanted), "releases": releases,
            "area_m2": area, "volume_m3": volume, "mean_mm": volume / area * 1000,
            "max_mm": float(total.max())}


def release_summary(days, releases):
    """One line naming each day's PRISM release, grouping runs of days, e.g. 'r2 Oct 20-25, r1 Oct 26-27'."""
    parts, i = [], 0
    while i < len(days):
        j = i
        while j + 1 < len(days) and releases[j + 1] == releases[i]:
            j += 1
        span = days[i].strftime("%b %d") + (f"-{days[j].strftime('%b %d')}" if j > i else "")
        parts.append(f"{'r' + str(releases[i]) if releases[i] else 'release unknown'} {span}")
        i = j + 1
    return ", ".join(parts)


def revision_change(history, days):
    """Window volume change from each day's first recorded PRISM release to its latest.

    history: rows of <var>_<res>_release_history.csv. Returns None unless every day is in the history;
    otherwise a dict with the first and latest summed daily volumes (m³), their change in percent, and
    the days revised as [(day, first release, latest release)].
    """
    by_day = {}
    for r in history:
        by_day.setdefault(r["date"], []).append(r)
    first = latest = 0.0
    revised = []
    for d in days:
        rows = sorted(by_day.get(str(d), []), key=lambda r: int(r["release"]) if r["release"].isdigit() else 0)
        if not rows:
            return None
        first += float(rows[0]["basin_volume_m3"])
        latest += float(rows[-1]["basin_volume_m3"])
        if len(rows) > 1:
            revised.append((d, rows[0]["release"], rows[-1]["release"]))
    return {"first_m3": first, "latest_m3": latest,
            "change_pct": (latest - first) / first * 100 if first else 0.0, "revised": revised}


def cell_fractions(geom, transform, shape):
    """Share of each grid cell inside geom (lat/lon polygon on the grid's CRS); 0 for cells it misses.

    Shares are taken in degree space, which within one 1/120-degree cell matches the ellipsoidal share
    to well under 0.1%.
    """
    touched = features.geometry_mask([geom], shape, transform, all_touched=True, invert=True)
    rows, cols = np.nonzero(touched)
    x0 = transform.c + cols * transform.a
    y0 = transform.f + rows * transform.e
    boxes = shapely.box(x0, y0 + transform.e, x0 + transform.a, y0)
    shapely.prepare(geom)
    frac = np.ones(len(boxes))
    edge = ~shapely.contains_properly(geom, boxes)
    frac[edge] = shapely.area(shapely.intersection(boxes[edge], geom)) / abs(transform.a * transform.e)
    out = np.zeros(shape)
    out[rows, cols] = frac
    return out


def boundary_volumes(total_mm, transform, geom):
    """Window volume (m³) over geom three ways, from a window-total depth grid (NaN = no data).

    centers   cells whose centers fall inside geom (the rule the cube uses)
    touched   every cell geom touches
    weighted  every cell weighted by its share inside geom
    Cells without PRISM data are left out of all three.
    """
    shape = total_mm.shape
    area = np.repeat(cell_area_m2(transform, shape[0])[:, None], shape[1], axis=1)
    cell_m3 = np.where(np.isnan(total_mm), 0.0, total_mm) / 1000 * area
    centers = features.geometry_mask([geom], shape, transform, invert=True)
    touched = features.geometry_mask([geom], shape, transform, all_touched=True, invert=True)
    return {"centers": float(cell_m3[centers].sum()), "touched": float(cell_m3[touched].sum()),
            "weighted": float((cell_m3 * cell_fractions(geom, transform, shape)).sum())}


def national_window_total(grids, ds):
    """Sum the national grids over the cube's extent plus one cell each side, so every cell the basin
    touches is included. Returns (total mm with NaN for nodata, transform)."""
    t = cube_transform(ds)
    h, w = ds.sizes["lat"], ds.sizes["lon"]
    left, top = t.c - t.a, t.f - t.e
    right, bottom = t.c + (w + 1) * t.a, t.f + (h + 1) * t.e
    total, transform = None, None
    for path in grids:
        with rasterio.open(path) as src:
            win = from_bounds(left, bottom, right, top, src.transform).round_offsets().round_lengths()
            arr = src.read(1, window=win, masked=True, boundless=True).astype("float64").filled(np.nan)
            transform = src.window_transform(win)
        total = arr if total is None else total + arr
    return total, transform


def cube_transform(ds):
    """Affine transform of the cube's (lat, lon) cell grid."""
    lat, lon = ds.lat.values, ds.lon.values
    dx, dy = lon[1] - lon[0], lat[0] - lat[1]
    return from_origin(lon[0] - dx / 2, lat[0] + dy / 2, dx, dy)


def zone_totals(ds, geoms, start, end, var="ppt"):
    """Depth and volume over [start, end] for each polygon (shapely, EPSG:4269), from the cube.

    Cells count when their centers fall inside a polygon. Cells without PRISM data (outside the
    clipped basin, or outside PRISM coverage) are left out of depth and volume and reported as
    coverage, so polygon volumes add up to the basin total. Returns one dict per polygon.
    """
    from rasterio import features
    r = window_total(ds, start, end, var)
    total = r["total_mm"].values
    area = ds.cell_area.values
    transform = cube_transform(ds)
    out = []
    for geom in geoms:
        inside = features.geometry_mask([geom], total.shape, transform, invert=True)
        ok = inside & ~np.isnan(total)
        cells_m2, data_m2 = float(area[inside].sum()), float(area[ok].sum())
        volume = float((total[ok] / 1000 * area[ok]).sum())
        out.append({"cells_km2": cells_m2 / 1e6, "area_km2": data_m2 / 1e6,
                    "coverage_pct": data_m2 / cells_m2 * 100 if cells_m2 else 0.0,
                    "mean_mm": volume / data_m2 * 1000 if data_m2 else float("nan"),
                    "max_mm": float(total[ok].max()) if ok.any() else float("nan"),
                    "volume_m3": volume})
    return out


def write_tif(grid, crs_wkt, path, units):
    """Write a (lat, lon) DataArray to a float32 GeoTIFF (nodata -9999)."""
    lat, lon = grid.lat.values, grid.lon.values
    dx, dy = lon[1] - lon[0], lat[0] - lat[1]
    transform = from_origin(lon[0] - dx / 2, lat[0] + dy / 2, dx, dy)
    data = np.nan_to_num(grid.values.astype("float32"), nan=-9999)
    with rasterio.open(path, "w", driver="GTiff", height=data.shape[0], width=data.shape[1],
                       count=1, dtype="float32", crs=crs_wkt, transform=transform,
                       nodata=-9999, compress="deflate") as dst:
        dst.write(data, 1)
        dst.set_band_unit(1, units)


def write_volume_nc(ds, r, path):
    """Write daily and window-total water volume per cell (m³) to NetCDF."""
    grid = {"units": "m3", "grid_mapping": "crs"}
    out = xr.Dataset(
        {
            "volume": (r["daily_mm"] / 1000 * ds.cell_area).assign_attrs(
                long_name="daily water volume per cell (depth x cell area)", **grid),
            "volume_total": r["volume_cell_m3"].assign_attrs(
                long_name="window-total water volume per cell", **grid),
            "cell_area": ds.cell_area,
            "crs": ds.crs,
        },
        attrs={**ds.attrs, "basin_volume_m3": r["volume_m3"]},
    )
    enc = {"zlib": True, "complevel": 4, "_FillValue": np.nan}
    out.to_netcdf(path, encoding={"volume": enc, "volume_total": enc})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="folder prism_watershed.py wrote to")
    ap.add_argument("--res", default="800m", choices=["800m", "4km"])
    ap.add_argument("--start", help="YYYY-MM-DD, first PRISM day (default: first day in the cube)")
    ap.add_argument("--end", help="YYYY-MM-DD, last PRISM day (default: last day in the cube)")
    ap.add_argument("--tif", help="also write the window's total depth (mm) to this GeoTIFF")
    ap.add_argument("--volume-tif",
                    help="also write the water volume per cell (m³ = depth x cell area) to this GeoTIFF")
    ap.add_argument("--volume-nc",
                    help="also write daily and total water volume per cell (m³) to this NetCDF")
    ap.add_argument("--boundary",
                    help="basin polygon for the boundary check (default: the wbd_huc*.geojson in --out)")
    a = ap.parse_args()

    nc = Path(a.out) / f"ppt_{a.res}_daily_clip.nc"
    if not nc.exists():
        sys.exit(f"No cube at {nc}. Run prism_watershed.py with --out {a.out} first.")
    with xr.open_dataset(nc) as ds:
        days = ds.time.values.astype("datetime64[D]")
        start = a.start or str(days.min())
        end = a.end or str(days.max())
        r = window_total(ds, start, end)
        if a.tif:
            write_tif(r["total_mm"], ds.crs.attrs["crs_wkt"], a.tif, "mm")
        if a.volume_tif:
            write_tif(r["volume_cell_m3"], ds.crs.attrs["crs_wkt"], a.volume_tif, "m3")
        if a.volume_nc:
            write_volume_nc(ds, r, a.volume_nc)
        gap = ds.attrs.get("basin_without_prism_pct", 0)
        basin_km2 = ds.attrs.get("basin_area_km2")
        bounds = boundary_check(Path(a.out), a.res, a.boundary, ds, start, r["days"])

    v = r["volume_m3"]
    print(f"PRISM days {start} to {end} ({r['days']} days; each ends 12:00 UTC on its date)")
    print(f"Basin area:  {r['area_m2'] / 1e6:,.0f} km²"
          + (f" with PRISM data, of {basin_km2:,.0f} km² ({gap:.1f}% has no PRISM data, usually outside the "
             "conterminous US; depth and volume cover only the area with data)" if gap > 0.5 else ""))
    print(f"Mean depth:  {r['mean_mm']:.1f} mm ({r['mean_mm'] / 25.4:.2f} in)")
    print(f"Max depth:   {r['max_mm']:.1f} mm ({r['max_mm'] / 25.4:.2f} in)")
    print(f"Volume:      {v:.4g} m³ = {v / 1e9:.3f} km³ = {v / M3_PER_ACRE_FT:,.0f} acre-ft")
    days = [dt.date.fromisoformat(start) + dt.timedelta(days=i) for i in range(r["days"])]
    print(f"Releases:    {release_summary(days, r['releases'])}")
    if any(rel < 8 for rel in r["releases"]):
        print("             Release 8 is final; earlier releases change for about 6 months. To pick up revisions, "
              "re-run prism_watershed.py with --refresh.")
    history_csv = Path(a.out) / f"ppt_{a.res}_release_history.csv"
    if history_csv.exists():
        with open(history_csv, newline="") as f:
            rc = revision_change(list(csv.DictReader(f)), days)
        if rc and rc["revised"]:
            print(f"Revisions:   window volume {rc['change_pct']:+.1f}% since the first releases recorded "
                  f"({rc['first_m3']:.4g} -> {rc['latest_m3']:.4g} m³); revised: "
                  + ", ".join(f"{d.strftime('%b %d')} r{r0}->r{r1}" for d, r0, r1 in rc["revised"]))
        elif rc:
            print(f"Revisions:   none recorded yet for these days ({history_csv.name})")
    if isinstance(bounds, str):
        print(f"Boundary:    {bounds}")
    else:
        lo, hi = min(bounds.values()), max(bounds.values())
        print(f"Boundary:    cell centers {bounds['centers'] / 1e9:.4f} km³, area-weighted {bounds['weighted'] / 1e9:.4f}, "
              f"any cell touched {bounds['touched'] / 1e9:.4f} (range {(hi - lo) / bounds['weighted'] * 100:.2f}% "
              "of area-weighted)")
    if a.tif:
        print(f"Total depth raster: {a.tif}")
    if a.volume_tif:
        print(f"Volume raster (m³ per cell): {a.volume_tif}")
    if a.volume_nc:
        print(f"Volume NetCDF (daily + total m³ per cell): {a.volume_nc}")


def boundary_check(out, res, boundary, ds, start, n_days):
    """boundary_volumes for the window from the national grids in <out>/extracted, or a note saying why
    it was skipped."""
    import geopandas as gpd
    files = [Path(boundary)] if boundary else sorted(out.glob("wbd_huc*.geojson"))
    if len(files) != 1:
        return "skipped: pass --boundary (no single wbd_huc*.geojson in --out)"
    days = [dt.date.fromisoformat(start) + dt.timedelta(days=i) for i in range(n_days)]
    grids = [prism_day_files(out / "extracted", "ppt", res, d)[0] for d in days]
    if None in grids:
        return "skipped: national grids for the window are not all in extracted/"
    geom = gpd.read_file(files[0]).to_crs(4269).union_all()
    total, transform = national_window_total(grids, ds)
    return boundary_volumes(total, transform, geom)


if __name__ == "__main__":
    main()
