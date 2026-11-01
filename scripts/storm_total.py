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

Rain and snow: if <out>/tmean_<res>_daily_clip.nc exists (prism_watershed.py --var tmean
for the same basin and days), each cell's daily precipitation is split by its PRISM daily
mean temperature: all snow at or below --all-snow-c (default 0 °C), all rain at or above
--all-rain-c (default 3 °C), linear between (equal values give a single threshold). Snow
here is snow-water equivalent; rain + snow = precipitation.

Usage examples:
    python storm_total.py --out data/huc14080104                     # every day in the cube
    python storm_total.py --out data/huc14080104 --start 2026-10-22 --end 2026-10-23
    python storm_total.py --out data/huc14080104 --tif depth_mm.tif
    python storm_total.py --out data/huc14080104 --volume-tif volume_m3.tif
    python storm_total.py --out data/huc14080104 --volume-nc volume_m3.nc
    python storm_total.py --out data/huc14080104 --rain-tif rain_mm.tif --snow-tif snow_mm.tif --rain-snow-nc rain_snow.nc

The volume raster holds m³ of water per cell; its sum is the basin volume. The
volume NetCDF holds the same per day (`volume`) plus the window total (`volume_total`).

Uncertainty: the report ends with the volume's components of uncertainty (gauge comparison
from validate_stations.py for the same window, basin boundary, PRISM revisions so far; other
products not yet assessed) and a bounding range, saved to
<out>/ppt_<res>_uncertainty_<start>_<end>.json. With a tmean cube, the rain volume is also
given across --ramp-alternatives. Method: docs/UNCERTAINTY.md.

Each run ends with QC checks for the window (qc.py; --qc-strict turns warnings into
failures, and any failure gives exit status 1) and appends a record of the run to
<out>/run_manifest.json.
"""
import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio import features
from rasterio.transform import from_origin
from rasterio.windows import from_bounds
import shapely
import xarray as xr

import manifest
import qc
import uncertainty

# PRISM file names carry a resolution code instead of the resolution: 30s = 800 m, 25m = 4 km.
RES_CODES = {"800m": "30s", "4km": "25m"}
GRS80_A = 6378137.0
GRS80_F = 1 / 298.257222101
M3_PER_ACRE_FT = 1233.48184
GRID_SUFFIXES = (".tif", ".bil", ".nc", ".asc")  # preference order
VAR_UNITS = {"ppt": "mm", "tmin": "degC", "tmax": "degC", "tmean": "degC", "tdmean": "degC",
             "vpdmin": "hPa", "vpdmax": "hPa"}
# CF standard names and cell methods per PRISM variable (vpd has no CF standard name).
VAR_CF = {"ppt": ("lwe_thickness_of_precipitation_amount", "time: sum"),
          "tmin": ("air_temperature", "time: minimum"), "tmax": ("air_temperature", "time: maximum"),
          "tmean": ("air_temperature", "time: mean (mean of daily maximum and minimum)"),
          "tdmean": ("dew_point_temperature", "time: mean"),
          "vpdmin": (None, "time: minimum"), "vpdmax": (None, "time: maximum")}


def cf_time(ds_or_days):
    """CF time attributes and bounds for PRISM days: each time value is 00:00 UTC on the day label, the
    midpoint of the PRISM day (12:00 UTC the day before to 12:00 UTC on the label date)."""
    days = np.asarray(ds_or_days, dtype="datetime64[ns]")
    bnds = np.stack([days - np.timedelta64(12, "h"), days + np.timedelta64(12, "h")], axis=1)
    attrs = {"standard_name": "time", "axis": "T", "bounds": "time_bnds",
             "long_name": "PRISM day midpoint (each day is the 24 h ending 12:00 UTC on its date)"}
    return attrs, bnds


def cf_globals(title):
    """Global attributes shared by the NetCDF outputs (CF-1.10)."""
    return {"Conventions": "CF-1.10", "title": title,
            "institution": "Data: PRISM Group, Oregon State University; processing: PRISM Data Wrangler",
            "source": "PRISM Group, Oregon State University, https://prism.oregonstate.edu",
            "references": "https://prism.oregonstate.edu/documents/PRISM_datasets.pdf",
            "history": f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} written by PRISM Data Wrangler",
            "prism_day": "Each time step covers 12:00 UTC the previous day to 12:00 UTC its date."}


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


def area_grid(transform, shape):
    """Ellipsoidal area (m^2) of every cell of a (rows, cols) north-up lat/lon grid."""
    return np.repeat(cell_area_m2(transform, shape[0])[:, None], shape[1], axis=1)


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
    area = area_grid(transform, (h, w))
    std, method = VAR_CF.get(var, (None, ""))
    t_attrs, t_bnds = cf_time(days)
    ds = xr.Dataset(
        {
            var: (("time", "lat", "lon"), np.stack(layers),
                  {"units": VAR_UNITS.get(var, ""), "long_name": f"PRISM daily {var}", "grid_mapping": "crs",
                   "cell_methods": f"area: mean {method}".strip(), "cell_measures": "area: cell_area",
                   **({"standard_name": std} if std else {})}),
            "time_bnds": (("time", "nv"), t_bnds),
            "cell_area": (("lat", "lon"), area,
                          {"units": "m2", "standard_name": "cell_area", "long_name": "cell area on the GRS80 ellipsoid",
                           "grid_mapping": "crs"}),
            "release": (("time",), np.array(releases, dtype="int8"),
                        {"long_name": "PRISM dataset release number (1-8, 8 final; 0 unknown)"}),
            "crs": ((), np.int32(0), {"grid_mapping_name": "latitude_longitude", "semi_major_axis": GRS80_A,
                                      "inverse_flattening": 1 / GRS80_F,
                                      "crs_wkt": crs.to_wkt(), "spatial_ref": crs.to_wkt()}),
        },
        coords={
            "time": ("time", days, t_attrs),
            "lat": ("lat", lat, {"units": "degrees_north", "standard_name": "latitude", "axis": "Y"}),
            "lon": ("lon", lon, {"units": "degrees_east", "standard_name": "longitude", "axis": "X"}),
        },
        attrs={**cf_globals(f"PRISM daily {var} ({res}) clipped to a basin"), **(attrs or {})},
    )
    ds.to_netcdf(nc_path, encoding={var: {"zlib": True, "complevel": 4, "_FillValue": np.nan},
                                    "time": {"units": "days since 1970-01-01 00:00:00", "dtype": "float64"},
                                    "time_bnds": {"units": "days since 1970-01-01 00:00:00", "dtype": "float64"}})
    return nc_path


def cf_extras(ds, daily):
    """time_bnds and time attributes for a NetCDF holding some of the cube's days."""
    t_attrs, t_bnds = cf_time(daily.time.values)
    return {"time_bnds": (("time", "nv"), t_bnds)}, t_attrs


TIME_ENC = {"time": {"units": "days since 1970-01-01 00:00:00", "dtype": "float64"},
            "time_bnds": {"units": "days since 1970-01-01 00:00:00", "dtype": "float64"}}


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


def geodesic_km2(geom):
    """Ellipsoidal (GRS80) area of a lon/lat polygon."""
    from pyproj import Geod
    area, _ = Geod(ellps="GRS80").geometry_area_perimeter(geom)
    return abs(area) / 1e6


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
    area = area_grid(transform, shape)
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


def snow_fraction(t, all_snow_c=0.0, all_rain_c=3.0):
    """Share of precipitation falling as snow, from daily mean temperature t (°C, numpy array).

    1 at or below all_snow_c, 0 at or above all_rain_c, linear between. If all_rain_c <= all_snow_c,
    a single threshold: snow below all_snow_c, rain at or above it. NaN where t is NaN.
    """
    t = np.asarray(t, dtype=float)
    if all_rain_c <= all_snow_c:
        f = (t < all_snow_c).astype(float)
    else:
        f = np.clip((all_rain_c - t) / (all_rain_c - all_snow_c), 0.0, 1.0)
    return np.where(np.isnan(t), np.nan, f)


def rain_snow(daily_mm, tmean, all_snow_c=0.0, all_rain_c=3.0):
    """Split daily precipitation (DataArray: time, lat, lon) into rain and snow using tmean on the same
    grid and days. snow = precipitation x snow fraction and rain = precipitation - snow, so they add up to
    the precipitation. Returns (rain, snow, snow fraction) as DataArrays like daily_mm."""
    f = snow_fraction(tmean, all_snow_c, all_rain_c)
    snow = daily_mm.values * f
    return daily_mm.copy(data=daily_mm.values - snow), daily_mm.copy(data=snow), daily_mm.copy(data=f)


def partition_window(out, res, ds, r, all_snow_c, all_rain_c):
    """Rain and snow for a window_total result r, from the tmean cube in out, or a note why not."""
    tnc = Path(out) / f"tmean_{res}_daily_clip.nc"
    if not tnc.exists():
        return f"skipped: no {tnc.name} (run prism_watershed.py --var tmean for the same basin and days)"
    daily = r["daily_mm"]
    with xr.open_dataset(tnc) as tds:
        if tds.sizes["lat"] != ds.sizes["lat"] or tds.sizes["lon"] != ds.sizes["lon"] or not (
                np.allclose(tds.lat, ds.lat) and np.allclose(tds.lon, ds.lon)):
            return f"skipped: {tnc.name} is on a different grid than the precipitation cube"
        have = set(tds.time.values.astype("datetime64[D]"))
        missing = [str(t)[:10] for t in daily.time.values.astype("datetime64[D]") if t not in have]
        if missing:
            return f"skipped: {tnc.name} has no data for {', '.join(missing)}"
        tmean = tds.tmean.sel(time=daily.time).values
        t_releases = ([int(v) for v in tds.release.sel(time=daily.time).values] if "release" in tds else [])
    rain, snow, frac = rain_snow(daily, tmean, all_snow_c, all_rain_c)
    inside = r["total_mm"].notnull()
    out_r = {"rain_mm": rain, "snow_mm": snow, "snow_fraction": frac, "tmean_releases": t_releases}
    for name, da in (("rain", rain), ("snow", snow)):
        total = da.sum("time", skipna=False)
        out_r[f"{name}_total_mm"] = total
        out_r[f"{name}_m3"] = float((total / 1000 * ds.cell_area).where(inside).sum())
    return out_r


def with_attrs(da, **attrs):
    """A copy of da with exactly these attributes (none inherited from the precipitation it came from)."""
    out = da.copy()
    out.attrs = attrs
    return out


def write_rain_snow_nc(ds, part, path, all_snow_c, all_rain_c):
    """Daily rain, snow and snow fraction plus window totals (mm) to NetCDF."""
    grid = {"grid_mapping": "crs", "cell_measures": "area: cell_area"}
    bnds, t_attrs = cf_extras(ds, part["rain_mm"])
    out = xr.Dataset(
        {
            "rain": with_attrs(part["rain_mm"], units="mm", standard_name="thickness_of_rainfall_amount",
                               long_name="daily rain", cell_methods="area: mean time: sum", **grid),
            "snow": with_attrs(part["snow_mm"], units="mm", standard_name="lwe_thickness_of_snowfall_amount",
                               long_name="daily snow (water equivalent)", cell_methods="area: mean time: sum", **grid),
            "snow_fraction": with_attrs(part["snow_fraction"], units="1", long_name="share of precipitation as snow",
                                        **grid),
            "rain_total": with_attrs(part["rain_total_mm"], units="mm", standard_name="thickness_of_rainfall_amount",
                                     long_name="window-total rain", **grid),
            "snow_total": with_attrs(part["snow_total_mm"], units="mm",
                                     standard_name="lwe_thickness_of_snowfall_amount",
                                     long_name="window-total snow (water equivalent)", **grid),
            "cell_area": ds.cell_area,
            "crs": ds.crs,
            **bnds,
        },
        attrs={**ds.attrs, **cf_globals("Rain and snow from PRISM precipitation and daily mean temperature"),
               "rain_snow_method": f"snow fraction 1 at or below {all_snow_c:g} degC daily mean "
               f"temperature (PRISM tmean), 0 at or above {all_rain_c:g} degC, linear between",
               "rain_m3": part["rain_m3"], "snow_m3": part["snow_m3"]},
    )
    out["time"].attrs.update(t_attrs)
    enc = {"zlib": True, "complevel": 4, "_FillValue": np.nan}
    out.to_netcdf(path, encoding={**{k: enc for k in ("rain", "snow", "snow_fraction", "rain_total", "snow_total")},
                                  **TIME_ENC})


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
    grid = {"units": "m3", "grid_mapping": "crs", "cell_measures": "area: cell_area"}
    bnds, t_attrs = cf_extras(ds, r["daily_mm"])
    out = xr.Dataset(
        {
            "volume": (r["daily_mm"] / 1000 * ds.cell_area).assign_attrs(
                long_name="daily water volume per cell (depth x cell area)", cell_methods="area: sum time: sum",
                **grid),
            "volume_total": r["volume_cell_m3"].assign_attrs(
                long_name="window-total water volume per cell", cell_methods="area: sum", **grid),
            "cell_area": ds.cell_area,
            "crs": ds.crs,
            **bnds,
        },
        attrs={**ds.attrs, **cf_globals("Water volume per PRISM cell"), "basin_volume_m3": r["volume_m3"]},
    )
    out["time"].attrs.update(t_attrs)
    enc = {"zlib": True, "complevel": 4, "_FillValue": np.nan}
    out.to_netcdf(path, encoding={"volume": enc, "volume_total": enc, **TIME_ENC})


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
    ap.add_argument("--all-snow-c", type=float, default=0.0,
                    help="daily mean temperature at or below which precipitation is all snow (default 0 °C)")
    ap.add_argument("--all-rain-c", type=float, default=3.0,
                    help="daily mean temperature at or above which precipitation is all rain (default 3 °C)")
    ap.add_argument("--rain-tif", help="also write the window's total rain (mm) to this GeoTIFF (needs the tmean cube)")
    ap.add_argument("--snow-tif", help="also write the window's total snow-water (mm) to this GeoTIFF")
    ap.add_argument("--rain-snow-nc", help="also write daily rain, snow and snow fraction plus totals to this NetCDF")
    ap.add_argument("--ramp-alternatives", default="-1,3 1,1",
                    help="other rain/snow ramps (all-snow,all-rain °C pairs) for the rain volume range (default '-1,3 1,1')")
    ap.add_argument("--qc-strict", action="store_true", help="treat QC warnings as failures")
    ap.add_argument("--qc-max-daily-mm", type=float, default=400.0,
                    help="flag daily precipitation in any cell above this (default 400 mm)")
    a = ap.parse_args()
    run = manifest.start_run("storm_total.py", a)

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
        part = partition_window(a.out, a.res, ds, r, a.all_snow_c, a.all_rain_c)
        if isinstance(part, str) and (a.rain_tif or a.snow_tif or a.rain_snow_nc):
            sys.exit(f"Rain/snow outputs requested but the partition was {part}")
        alt_rain = {}
        if not isinstance(part, str):
            for pair in a.ramp_alternatives.split():
                t_snow, t_rain = (float(x) for x in pair.split(","))
                alt = partition_window(a.out, a.res, ds, r, t_snow, t_rain)
                alt_rain[pair] = alt["rain_m3"]
            if a.rain_tif:
                write_tif(part["rain_total_mm"], ds.crs.attrs["crs_wkt"], a.rain_tif, "mm")
            if a.snow_tif:
                write_tif(part["snow_total_mm"], ds.crs.attrs["crs_wkt"], a.snow_tif, "mm")
            if a.rain_snow_nc:
                write_rain_snow_nc(ds, part, a.rain_snow_nc, a.all_snow_c, a.all_rain_c)
        daily = r["daily_mm"]
        day_keys = [str(t)[:10] for t in daily.time.values]
        day_min = dict(zip(day_keys, daily.min(("lat", "lon")).values.tolist()))
        day_max = dict(zip(day_keys, daily.max(("lat", "lon")).values.tolist()))
        has_data = daily.notnull()
        some, every = has_data.any("time"), has_data.all("time")
        some_m2 = float(ds.cell_area.where(some).sum())
        varying = {k: float(ds.cell_area.where(some & ~has_data.isel(time=i)).sum()) / some_m2 * 100
                   for i, k in enumerate(day_keys)} if some_m2 else {}

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
    rc = None
    if history_csv.exists():
        with open(history_csv, newline="") as f:
            rc = revision_change(list(csv.DictReader(f)), days)
        if rc and rc["revised"]:
            print(f"Revisions:   window volume {rc['change_pct']:+.1f}% since the first releases recorded "
                  f"({rc['first_m3']:.4g} -> {rc['latest_m3']:.4g} m³); revised: "
                  + ", ".join(f"{d.strftime('%b %d')} r{r0}->r{r1}" for d, r0, r1 in rc["revised"]))
        elif rc:
            print(f"Revisions:   none recorded yet for these days ({history_csv.name})")
    if isinstance(part, str):
        print(f"Rain/snow:   {part}")
    else:
        rv, sv = part["rain_m3"], part["snow_m3"]
        ramp = (f"snow below {a.all_snow_c:g} °C" if a.all_rain_c <= a.all_snow_c else
                f"all snow at or below {a.all_snow_c:g} °C, all rain at or above {a.all_rain_c:g} °C")
        pct = (lambda x: f"{x / v * 100:.1f}%") if v else (lambda x: "n/a")
        print(f"Rain/snow:   rain {rv / r['area_m2'] * 1000:.1f} mm, {rv / 1e9:.3f} km³ ({pct(rv)}); "
              f"snow {sv / r['area_m2'] * 1000:.1f} mm, {sv / 1e9:.3f} km³ ({pct(sv)}) "
              f"[{ramp}, daily mean temperature]")
    if isinstance(bounds, str):
        print(f"Boundary:    {bounds}")
    else:
        vols = bounds["volumes"]
        lo, hi = min(vols.values()), max(vols.values())
        spread = f"{(hi - lo) / vols['weighted'] * 100:.2f}%" if vols["weighted"] else "n/a"
        print(f"Boundary:    cell centers {vols['centers'] / 1e9:.4f} km³, area-weighted {vols['weighted'] / 1e9:.4f}, "
              f"any cell touched {vols['touched'] / 1e9:.4f} (range {spread} of area-weighted)")
    stations_json = Path(a.out) / "validation" / "stations_summary.json"
    event_totals = None
    if stations_json.exists():
        sj = json.loads(stations_json.read_text())
        if sj.get("window", {}).get("start") == start and sj["window"].get("end") == end:
            event_totals = sj.get("event_totals")
    components = [uncertainty.gauge_component(event_totals),
                  uncertainty.boundary_component(None if isinstance(bounds, str) else bounds["volumes"], v),
                  uncertainty.revision_component(rc, r["releases"]),
                  uncertainty.product_component()]
    print("\n".join(uncertainty.report(v, components)))
    v_lo, v_hi = uncertainty.volume_range(v, components)
    unc = {"window": {"start": start, "end": end}, "volume_m3": v, "range_m3": [v_lo, v_hi],
           "range_pct": list(uncertainty.bounding_range(components)), "components": components}
    if alt_rain:
        rains = [part["rain_m3"]] + list(alt_rain.values())
        unc["rain"] = {"rain_m3": part["rain_m3"], "range_m3": [min(rains), max(rains)],
                       "ramps": {f"{a.all_snow_c:g},{a.all_rain_c:g}": part["rain_m3"], **alt_rain}}
        print(f"  rain       {min(rains) / 1e9:.3f}-{max(rains) / 1e9:.3f} km³ across ramps "
              f"{a.all_snow_c:g},{a.all_rain_c:g} (used) and {', '.join(alt_rain)} °C; shifts water between rain "
              "and snow, not the total")
    unc_json = Path(a.out) / f"ppt_{a.res}_uncertainty_{start.replace('-', '')}_{end.replace('-', '')}.json"
    unc_json.write_text(json.dumps(unc, indent=2))
    if a.tif:
        print(f"Total depth raster: {a.tif}")
    if a.volume_tif:
        print(f"Volume raster (m³ per cell): {a.volume_tif}")
    if a.volume_nc:
        print(f"Volume NetCDF (daily + total m³ per cell): {a.volume_nc}")
    for label, path in (("Rain raster (mm)", a.rain_tif), ("Snow raster (mm)", a.snow_tif),
                        ("Rain/snow NetCDF", a.rain_snow_nc)):
        if path:
            print(f"{label}: {path}")

    pairs = []
    if not isinstance(part, str):
        pairs.append(("rain + snow = precipitation volume", part["rain_m3"] + part["snow_m3"], v))
    if a.volume_tif:
        with rasterio.open(a.volume_tif) as src:
            pairs.append(("volume raster sum = reported volume", float(src.read(1, masked=True).astype("float64").sum()), v))
    if not isinstance(bounds, str):
        pairs.append(("national-grid volume by cell center = cube volume", bounds["volumes"]["centers"], v))
    checks = [
        qc.nodata_inside(varying, gap),
        qc.physical_limits(day_min, day_max, a.qc_max_daily_mm),
        qc.totals_reconcile(pairs),
        qc.release_mix(dict(zip(day_keys, r["releases"]))),
        (qc.result("boundary_agreement", "skip", bounds) if isinstance(bounds, str)
         else qc.boundary_agreement(bounds["grid_km2"], bounds["polygon_km2"])),
    ]
    if a.qc_strict:
        checks = qc.apply_strict(checks)
    print("\n" + "\n".join(qc.report(checks)))
    tnc = Path(a.out) / f"tmean_{a.res}_daily_clip.nc"
    run.update(window={"start": start, "end": end},
               inputs=manifest.files_entry([nc] + ([tnc] if not isinstance(part, str) else []), a.out),
               results={"area_km2": r["area_m2"] / 1e6, "mean_mm": r["mean_mm"], "max_mm": r["max_mm"],
                        "volume_m3": v, "releases": dict(zip(day_keys, r["releases"])),
                        "boundary_volumes_m3": None if isinstance(bounds, str) else bounds["volumes"],
                        "rain_snow": part if isinstance(part, str) else {
                            "rain_m3": part["rain_m3"], "snow_m3": part["snow_m3"], "all_snow_c": a.all_snow_c,
                            "all_rain_c": a.all_rain_c, "tmean_releases": part["tmean_releases"]},
                        "uncertainty": unc},
               qc=checks, outputs=manifest.files_entry([f for f in (a.tif, a.volume_tif, a.volume_nc, a.rain_tif,
                                                                    a.snow_tif, a.rain_snow_nc, unc_json) if f], a.out))
    print(f"Run recorded in {manifest.append_run(a.out, run).name}")
    if qc.worst_level(checks) == "fail":
        sys.exit("QC failed (see above).")


def boundary_check(out, res, boundary, ds, start, n_days):
    """{"volumes": boundary_volumes, "grid_km2": area of the cells counted by center, "polygon_km2"} for the
    window from the national grids in <out>/extracted, or a note saying why it was skipped."""
    import geopandas as gpd
    files = [Path(boundary)] if boundary else (sorted(out.glob("wbd_huc*.geojson")) or sorted(out.glob("boundary.geojson")))
    if len(files) != 1:
        return "skipped: pass --boundary (no single wbd_huc*.geojson or boundary.geojson in --out)"
    days = [dt.date.fromisoformat(start) + dt.timedelta(days=i) for i in range(n_days)]
    grids = [prism_day_files(out / "extracted", "ppt", res, d)[0] for d in days]
    if None in grids:
        return "skipped: national grids for the window are not all in extracted/"
    geom = gpd.read_file(files[0]).to_crs(4269).union_all()
    total, transform = national_window_total(grids, ds)
    centers = features.geometry_mask([geom], total.shape, transform, invert=True)
    area = area_grid(transform, total.shape)
    return {"volumes": boundary_volumes(total, transform, geom), "grid_km2": float(area[centers].sum()) / 1e6,
            "polygon_km2": geodesic_km2(geom)}


if __name__ == "__main__":
    main()
