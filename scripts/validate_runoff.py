#!/usr/bin/env python3
"""
Compare PRISM precipitation volume with streamflow volume at USGS gauges.

Gauges: --gauge for specific sites, or --all-gauges for every USGS stream gauge in
the study basin (the basin --out was built for) with a 15-minute discharge record
covering the window.

For each gauge, its drainage basin comes from USGS NLDI; PRISM precipitation over it is
taken from the national daily grids in <out>/extracted, so a gauge basin may extend past
the study basin (only the gauge itself must be inside it). Discharge comes from the gauge's 15-minute
record, which can survive when daily values are withheld (for example around an
equipment problem at the flood peak). Gaps are filled by linear interpolation and
reported; gaps longer than --max-gap-hours skip the gauge.

Runoff ratio = event runoff volume / precipitation volume. Event runoff is total flow
minus baseflow, bracketed two ways:
    constant   baseflow held at the flow at the start of the window (larger runoff)
    line       baseflow rising linearly from start-of-window to end-of-window flow
               (smaller runoff; the recession is still above pre-storm flow at the end)
Precipitation includes snow, which at high elevations stays on the ground, so ratios
from mountain basins understate rain-driven runoff. When PRISM tmean grids for the window
are in <out>/extracted (prism_watershed.py --var tmean), each gauge also gets a rain-only
ratio (runoff / rain volume), with rain split from snow by daily mean temperature as in
storm_total.py (--all-snow-c, --all-rain-c).

Regulation: dams from the National Inventory of Dams inside a gauge's basin are summed
(normal storage, else NID storage) and expressed as a depth over the basin, comparable
with storm precipitation depth:
    unregulated  < --minor-mm (default 1 mm)
    minor        --minor-mm to --regulated-mm (default 10 mm)
    regulated    >= --regulated-mm
A regulated gauge's runoff ratio reflects reservoir operations as much as precipitation.
Storage is capacity, not how full the reservoirs were; off-channel reservoirs and
transbasin diversions are not distinguished.

Flow window: 12:00 UTC the day before --start (the start of the first PRISM day) to
12:00 UTC --after-days after --end, to catch the recession (capped at the current time).

Outputs in <out>/validation/:
    validation.gpkg  layers 'gauge_basins' (basins with volumes, ratios, regulation),
                     'gauges' (every gauge considered, with status) and 'dams' (NID dams in
                     the study basin), with default QGIS styles
    runoff_summary.csv (one row per gauge), runoff_<site>.json, runoff_<site>_flow.csv

Usage examples:
    python validate_runoff.py --out data/huc1408 --all-gauges
    python validate_runoff.py --out data/huc1408 --gauge 09342500 09361500
    python validate_runoff.py --out data/huc1408 --all-gauges --start 2026-10-20 --end 2026-10-27 --after-days 10
"""
import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio import features
from rasterio.mask import mask as rio_mask
import requests
from shapely.geometry import Point
import xarray as xr

from storm_total import cell_area_m2, prism_day_files, snow_fraction
from validate_stations import load_basin, write_layer

CFS_TO_M3S = 0.028316846592
M3_PER_ACRE_FT = 1233.48184
KM2_PER_MI2 = 2.589988


class GaugeSkipped(Exception):
    """A gauge that cannot be compared; the message is the reason, .site its metadata if fetched.
    .stage is 'precip' when the reason comes from PRISM precipitation (so it can change with releases
    or settings), 'flow' when it comes from the discharge record, else ''."""

    def __init__(self, reason, site=None, stage=""):
        super().__init__(reason)
        self.site = site
        self.stage = stage


# ---------- processing (pure functions) ----------

def basin_precip(grids, basin, max_missing_pct=1.0, tmean=None, all_snow_c=0.0, all_rain_c=3.0):
    """Daily precipitation over a gauge basin from PRISM's national daily grids.

    grids: [(day, path)] of national grids (the ones in <out>/extracted), so a gauge basin may
    extend past the study basin. Cells count when their centers fall inside the basin; cell areas
    are ellipsoidal per row. Cells with no PRISM data (outside PRISM coverage, for example in
    Canada or Mexico) are filled with the basin-average depth, up to max_missing_pct of the basin
    area; beyond that a ValueError is raised.
    tmean: optional {day: national tmean grid}; each row then also has rain_mm and rain_m3 (precipitation
    minus snow by daily mean temperature, see storm_total.snow_fraction), averaged over cells with both grids.
    Returns (rows, missing_pct, basin area in m² from PRISM cells).
    """
    shapes = list(basin.to_crs(4269).geometry)
    rows, missing_pct, area_m2 = [], 0.0, 0.0
    for d, path in grids:
        with rasterio.open(path) as src:
            data, transform = rio_mask(src, shapes, crop=True, filled=True, nodata=src.nodata)
            nodata = src.nodata
        vals = data[0].astype(float)
        if nodata is not None:
            vals[vals == nodata] = np.nan
        inside = features.geometry_mask(shapes, vals.shape, transform, invert=True)
        area = np.repeat(cell_area_m2(transform, vals.shape[0])[:, None], vals.shape[1], axis=1)
        area_m2 = float(area[inside].sum())
        if area_m2 == 0:
            raise ValueError("gauge basin covers no PRISM grid cells")
        ok = inside & ~np.isnan(vals)
        missing_pct = max(missing_pct, float(area[inside & ~ok].sum() / area_m2 * 100))
        if missing_pct > max_missing_pct:
            raise ValueError(f"{missing_pct:.1f}% of the gauge basin is outside PRISM coverage "
                             f"(for example in Canada or Mexico) on {d}")
        depth = float((vals[ok] * area[ok]).sum() / area[ok].sum())
        rows.append({"prism_day": d, "depth_mm": depth, "volume_m3": depth / 1000 * area_m2})
        if tmean:
            with rasterio.open(tmean[d]) as src:
                t, _ = rio_mask(src, shapes, crop=True, filled=True, nodata=src.nodata)
                t = t[0].astype(float)
                if src.nodata is not None:
                    t[t == src.nodata] = np.nan
            both = ok & ~np.isnan(t)
            if not both.any():
                raise ValueError(f"no PRISM tmean data over the gauge basin on {d}")
            rain = vals * (1 - snow_fraction(t, all_snow_c, all_rain_c))
            rain_mm = float((rain[both] * area[both]).sum() / area[both].sum())
            rows[-1].update(rain_mm=rain_mm, rain_m3=rain_mm / 1000 * area_m2)
    return rows, missing_pct, area_m2


def flow_series(values, t0, t1):
    """Clean 15-minute values into (times, cfs) with gaps filled, plus a gap report.

    values: [(ISO time, value or None, qualifier, approval)] from the Water Data API.
    Returns (pandas Series indexed by UTC time on a regular 15-minute grid from t0 to t1,
             boolean Series marking interpolated steps, dict of gap info).
    """
    times = pd.to_datetime([v[0] for v in values], utc=True)
    q = pd.Series([float(v[1]) if v[1] is not None else np.nan for v in values], index=times)
    q = q[~q.index.duplicated()].sort_index()
    valid = q.dropna()
    if valid.empty:
        raise ValueError("No discharge values in the flow window.")
    grid = pd.date_range(t0, t1, freq="15min", tz="UTC")
    full = valid.reindex(valid.index.union(grid)).interpolate(method="time", limit_area="inside").reindex(grid)
    # A grid step is interpolated unless a reading lies within 7.5 minutes of it (readings need not
    # fall on :00/:15/:30/:45).
    pos = valid.index.searchsorted(grid)
    near = np.full(len(grid), np.inf)
    for k in (pos - 1, pos):
        ok = (k >= 0) & (k < len(valid))
        near[ok] = np.minimum(near[ok], np.abs((valid.index[k[ok]] - grid[ok]).total_seconds()))
    interpolated = near > 450
    # Gaps: consecutive readings further apart than the gauge's usual interval (15 min, or hourly, ...).
    steps = valid.index.to_series().diff().dt.total_seconds().div(3600)
    usual = float(steps.median()) if len(steps.dropna()) else 0.25
    gaps = steps[steps > max(0.26, usual * 1.01)]
    info = {"valid_readings": int(len(valid)), "reading_interval_min": round(usual * 60, 1),
            "longest_gap_h": float(gaps.max()) if len(gaps) else 0.0,
            "interpolated_h": float(interpolated.sum() * 0.25),
            "starts_after_window_h": max(0.0, (valid.index[0] - grid[0]).total_seconds() / 3600),
            "ends_before_window_h": max(0.0, (grid[-1] - valid.index[-1]).total_seconds() / 3600),
            "gaps": [{"after": str(valid.index[valid.index.get_loc(t) - 1]), "hours": round(h, 2)}
                     for t, h in gaps.items()]}
    return full, pd.Series(interpolated, index=grid), info


def runoff_volumes(q_cfs):
    """Total, baseflow and event-runoff volumes (m³) for a regular flow series in cfs."""
    q = q_cfs.to_numpy() * CFS_TO_M3S
    secs = (q_cfs.index - q_cfs.index[0]).total_seconds().to_numpy()
    total = float(np.trapezoid(q, secs))
    base_const = np.full_like(q, q[0])
    base_line = q[0] + (q[-1] - q[0]) * secs / secs[-1]
    out = {"total_flow_m3": total}
    for name, base in (("constant", base_const), ("line", base_line)):
        out[f"baseflow_{name}_m3"] = float(np.trapezoid(base, secs))
        out[f"runoff_{name}_m3"] = float(np.trapezoid(np.clip(q - base, 0, None), secs))
    return out, base_const / CFS_TO_M3S, base_line / CFS_TO_M3S


def regulation(basin, dams, area_m2, minor_mm=1.0, regulated_mm=10.0, precip_mm=None):
    """Upstream dam storage for a gauge basin and its regulation class.

    dams: list of dicts from observations.nid_dams. Capacity is normal storage, else NID storage, in
    acre-ft. A reservoir can hold back no more than the storm's precipitation over its own drainage area
    (NID DRAINAGE_AREA), so with precip_mm (the gauge basin's mean depth for the window) each dam's
    effective storage is min(capacity, precip_mm x its drainage area); dams with no drainage area keep
    their full capacity (conservative). The class uses effective storage as a depth over the gauge basin.
    Returns a dict with n_dams, storage_af/mm (capacity), effective_af/mm, class, the largest dams and
    the full list (so the class can be recomputed when the precipitation changes).
    """
    poly = basin.to_crs(4269).geometry.iloc[0]
    inside = [{"name": d["name"], "nidid": d["nidid"],
               "capacity_af": float(d.get("normal_storage_af") or d.get("nid_storage_af") or 0.0),
               "drainage_area_mi2": d.get("drainage_area_mi2")}
              for d in dams if poly.contains(Point(d["lon"], d["lat"]))]
    return regulation_from(inside, area_m2, minor_mm, regulated_mm, precip_mm)


def regulation_from(inside, area_m2, minor_mm=1.0, regulated_mm=10.0, precip_mm=None):
    """regulation() from the list of upstream dams it saved."""
    for d in inside:
        da = d.get("drainage_area_mi2")
        if precip_mm is not None and da:
            inflow_af = precip_mm / 1000 * da * KM2_PER_MI2 * 1e6 / M3_PER_ACRE_FT
            d["effective_af"] = min(d["capacity_af"], inflow_af)
        else:
            d["effective_af"] = d["capacity_af"]

    def depth(af):
        return af * M3_PER_ACRE_FT / area_m2 * 1000 if area_m2 else 0.0
    storage_af = float(sum(d["capacity_af"] for d in inside))
    effective_af = float(sum(d["effective_af"] for d in inside))
    effective_mm = depth(effective_af)
    cls = "regulated" if effective_mm >= regulated_mm else "minor" if effective_mm >= minor_mm else "unregulated"
    top = sorted(inside, key=lambda d: -d["effective_af"])[:3]
    return {"n_dams": len(inside), "storage_af": storage_af, "storage_mm": depth(storage_af),
            "effective_af": effective_af, "effective_mm": effective_mm, "class": cls,
            "n_no_drainage_area": sum(1 for d in inside if not d.get("drainage_area_mi2")),
            "largest": [{"name": d["name"], "nidid": d["nidid"], "storage_af": d["capacity_af"],
                         "effective_af": round(d["effective_af"])} for d in top if d["capacity_af"]],
            "dams": inside}


def basin_share_inside(basin, study_poly):
    """Fraction of a gauge basin's area that lies inside the study basin polygon (EPSG:4269)."""
    utm = basin.estimate_utm_crs()
    b = basin.to_crs(utm).geometry.union_all()
    study = gpd.GeoSeries([study_poly], crs=4269).to_crs(utm).iloc[0]
    return float(b.intersection(study).area / b.area) if b.area else 0.0


def upsert_layer(gdf, gpkg, layer, key, values):
    """Replace these gauges' rows in a GeoPackage layer, keeping other gauges."""
    if Path(gpkg).exists():
        try:
            old = gpd.read_file(gpkg, layer=layer)
            gdf = pd.concat([old[~old[key].isin(values)].to_crs(gdf.crs), gdf], ignore_index=True)
        except Exception:
            pass
    write_layer(gpd.GeoDataFrame(gdf, crs=gdf.crs), gpkg, layer)


# ---------- one gauge ----------

def run_gauge(site_no, site, grids, study_poly, dams_for, a, vdir, tmean=None):
    """Compare one gauge. Returns (result dict, basin GeoDataFrame); raises GaugeSkipped.

    dams_for(bounds) returns NID dams covering at least those lon/lat bounds (the gauge basin may extend
    past the study basin).
    """
    from observations import RateLimited, nldi_basin, usgs_continuous

    start, end = grids[0][0], grids[-1][0]
    if site.get("site_type") not in (None, "Stream"):
        raise GaugeSkipped(f"site type is {site['site_type']}, not a stream", site)
    if study_poly is not None and not study_poly.buffer(0.005).contains(Point(site["lon"], site["lat"])):
        raise GaugeSkipped("gauge is outside the study basin", site)
    try:
        basin = gpd.GeoDataFrame.from_features(nldi_basin(site_no)["features"], crs=4326).dissolve()
    except (RateLimited, requests.RequestException):
        raise  # transient: not saved as a skip, retried next run
    except Exception as e:
        raise GaugeSkipped(f"no NLDI basin ({e.__class__.__name__})", site)
    if study_poly is not None:
        share = basin_share_inside(basin, study_poly)
        if share < 0.5:
            raise GaugeSkipped(f"gauge basin lies mostly outside the study basin ({share:.0%} inside)", site)
    try:
        precip, missing_pct, grid_area_m2 = basin_precip(grids, basin, a.max_missing_pct, tmean,
                                                         a.all_snow_c, a.all_rain_c)
    except ValueError as e:
        raise GaugeSkipped(str(e), site, "precip")
    if not sum(r["volume_m3"] for r in precip):
        raise GaugeSkipped("no precipitation over the gauge basin in the window", site, "precip")

    t0 = dt.datetime.combine(start - dt.timedelta(days=1), dt.time(12), tzinfo=dt.timezone.utc)
    t1 = dt.datetime.combine(end + dt.timedelta(days=a.after_days), dt.time(12), tzinfo=dt.timezone.utc)
    t1 = min(t1, dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0))
    raw = usgs_continuous(site_no, t0.strftime("%Y-%m-%dT%H:%M:%SZ"), t1.strftime("%Y-%m-%dT%H:%M:%SZ"))
    try:
        q, interp, gaps = flow_series(raw, t0, t1)
    except ValueError as e:
        raise GaugeSkipped(str(e), site, "flow")
    worst = max(gaps["longest_gap_h"], gaps["starts_after_window_h"], gaps["ends_before_window_h"])
    if worst > a.max_gap_hours:
        raise GaugeSkipped(f"discharge gap of {worst:.1f} h (limit {a.max_gap_hours:g} h)", site, "flow")
    q = q.ffill().bfill()  # only the window edges, already checked against --max-gap-hours
    vols, base_c, base_l = runoff_volumes(q)
    p_depth = sum(r["volume_m3"] for r in precip) / grid_area_m2 * 1000
    reg = regulation(basin, dams_for(tuple(basin.to_crs(4269).total_bounds)), grid_area_m2, a.minor_mm,
                     a.regulated_mm, p_depth)

    peak_t = q.idxmax()
    result = {
        "gauge": site, "prism_window": {"start": str(start), "end": str(end)},
        "flow_window_utc": {"start": t0.isoformat(), "end": t1.isoformat(), "after_days": a.after_days},
        "basin_area_km2": {"nldi_polygon": round(float(basin.to_crs(basin.estimate_utm_crs()).area.sum() / 1e6), 1),
                           "prism_cells": round(grid_area_m2 / 1e6, 1),
                           "usgs_drainage_area": round(site["drainage_area_mi2"] * KM2_PER_MI2, 1)
                           if site.get("drainage_area_mi2") else None},
        **precip_and_ratios(precip, missing_pct, grid_area_m2, vols),
        "flow": {**vols, "peak_cfs": float(q.max()), "peak_time_utc": peak_t.isoformat(), **gaps,
                 "approval_status": pd.Series([v[3] for v in raw]).value_counts().to_dict(),
                 "qualifiers": pd.Series([", ".join(v[2]) for v in raw if v[2]]).value_counts().to_dict()},
        "regulation": reg,
        "notes": ["Precipitation includes snow; high-elevation snow does not run off within the window.",
                  "Discharge is provisional unless approval_status says otherwise.",
                  "Regulated gauges: reservoir storage and releases, not just precipitation, set the flow."]}
    (vdir / f"runoff_{site_no}.json").write_text(json.dumps(result, indent=2))
    basin.to_crs(4269).to_file(vdir / f"runoff_{site_no}_basin.geojson", driver="GeoJSON")
    pd.DataFrame({"q_cfs": q.round(1), "interpolated": interp, "baseflow_constant_cfs": np.round(base_c, 1),
                  "baseflow_line_cfs": np.round(base_l, 1)}).rename_axis("time_utc").to_csv(
        vdir / f"runoff_{site_no}_flow.csv")
    return result, basin


def precip_and_ratios(precip, missing_pct, grid_area_m2, vols):
    """The 'precip', 'rain' (if the rows have rain) and 'runoff_ratio' blocks of a gauge result."""
    p_vol = sum(r["volume_m3"] for r in precip)
    out = {"precip": {"volume_m3": p_vol, "depth_mm": p_vol / grid_area_m2 * 1000,
                      "filled_area_pct": round(missing_pct, 3),
                      "daily": [{**r, "prism_day": str(r["prism_day"])} for r in precip]},
           "rain": None,
           "runoff_ratio": {"constant_baseflow": vols["runoff_constant_m3"] / p_vol,
                            "line_baseflow": vols["runoff_line_m3"] / p_vol,
                            "total_flow": vols["total_flow_m3"] / p_vol}}
    if precip and "rain_m3" in precip[0]:
        r_vol = sum(r["rain_m3"] for r in precip)
        out["rain"] = {"volume_m3": r_vol, "depth_mm": r_vol / grid_area_m2 * 1000,
                       "snow_pct": (p_vol - r_vol) / p_vol * 100 if p_vol else 0.0}
        out["runoff_ratio"].update(rain_constant_baseflow=vols["runoff_constant_m3"] / r_vol if r_vol else None,
                                   rain_line_baseflow=vols["runoff_line_m3"] / r_vol if r_vol else None)
    return out


def update_precip(result, basin, grids, a, tmean=None):
    """Recompute a saved result's precipitation and runoff ratios from the current grids.

    Used when PRISM has revised the grids since the result was saved: the basin, flow and dams don't
    depend on the release, so no network requests are needed.
    """
    try:
        precip, missing_pct, grid_area_m2 = basin_precip(grids, basin, a.max_missing_pct, tmean,
                                                         a.all_snow_c, a.all_rain_c)
    except ValueError as e:
        raise GaugeSkipped(str(e), result["gauge"], "precip")
    if not sum(r["volume_m3"] for r in precip):
        raise GaugeSkipped("no precipitation over the gauge basin in the window", result["gauge"], "precip")
    result.update(precip_and_ratios(precip, missing_pct, grid_area_m2, result["flow"]))
    if "dams" in result.get("regulation", {}):  # effective storage depends on the precipitation
        result["regulation"] = regulation_from(result["regulation"]["dams"], grid_area_m2, a.minor_mm,
                                               a.regulated_mm, result["precip"]["depth_mm"])
    return result


def summary_row(site_no, result=None, site=None, reason=None):
    """One row for runoff_summary.csv and the gauge layers."""
    if result is None:
        s = site or {}
        return {"site": site_no, "name": s.get("name"), "status": reason, "regulation": None,
                "lon": s.get("lon"), "lat": s.get("lat")}
    rr, fl, reg = result["runoff_ratio"], result["flow"], result["regulation"]
    return {"site": site_no, "name": result["gauge"]["name"], "status": "ok", "regulation": reg["class"],
            "dam_storage_mm": round(reg["storage_mm"], 1), "dam_storage_af": round(reg["storage_af"]),
            "dam_effective_mm": round(reg.get("effective_mm", reg["storage_mm"]), 1),
            "storage_pct_of_precip": round(reg.get("effective_mm", reg["storage_mm"]) / result["precip"]["depth_mm"]
                                           * 100, 1),
            "n_dams": reg["n_dams"],
            "largest_dams": "; ".join(f"{d['name']} ({d['storage_af']:,.0f} af"
                                      + (f", {d['effective_af']:,.0f} effective)" if "effective_af" in d else ")")
                                      for d in reg["largest"]),
            "area_km2": result["basin_area_km2"]["prism_cells"],
            "usgs_area_km2": result["basin_area_km2"]["usgs_drainage_area"],
            "prism_start": result["prism_window"]["start"], "prism_end": result["prism_window"]["end"],
            "precip_mm": round(result["precip"]["depth_mm"], 1), "precip_m3": round(result["precip"]["volume_m3"]),
            "total_flow_m3": round(fl["total_flow_m3"]), "runoff_line_m3": round(fl["runoff_line_m3"]),
            "runoff_const_m3": round(fl["runoff_constant_m3"]),
            "ratio_line": round(rr["line_baseflow"], 3), "ratio_const": round(rr["constant_baseflow"], 3),
            "ratio_low": round(min(rr["line_baseflow"], rr["constant_baseflow"]), 3),
            "ratio_high": round(max(rr["line_baseflow"], rr["constant_baseflow"]), 3),
            **({"rain_mm": round(result["rain"]["depth_mm"], 1), "snow_pct": round(result["rain"]["snow_pct"], 1),
                "rain_ratio_low": round(min(rr["rain_line_baseflow"], rr["rain_constant_baseflow"]), 3)
                if rr.get("rain_line_baseflow") is not None else None,
                "rain_ratio_high": round(max(rr["rain_line_baseflow"], rr["rain_constant_baseflow"]), 3)
                if rr.get("rain_line_baseflow") is not None else None} if result.get("rain") else {}),
            "flow_complete": result.get("flow_complete", True),
            "peak_cfs": round(fl["peak_cfs"]), "peak_utc": fl["peak_time_utc"],
            "interp_h": round(fl["interpolated_h"], 2), "flow_end_utc": result["flow_window_utc"]["end"],
            "lon": result["gauge"]["lon"], "lat": result["gauge"]["lat"]}


def window_key(days, a, releases=(), rain_snow=None, tmean_releases=()):
    """Identifies a run's settings, so cached gauge results are reused only for the same window."""
    return {"start": str(days[0]), "end": str(days[-1]), "after_days": a.after_days, "precip": "national grids",
            "max_gap_hours": a.max_gap_hours, "minor_mm": a.minor_mm, "regulated_mm": a.regulated_mm,
            "res": a.res, "max_missing_pct": a.max_missing_pct, "releases": list(releases),
            "regulation": "effective storage (capped by precipitation over each dam's drainage area)",
            "rain_snow": rain_snow, "tmean_releases": list(tmean_releases)}


# Settings that change only the precipitation, not the basin, flow or dams.
PRECIP_KEYS = ("res", "max_missing_pct", "releases", "rain_snow", "tmean_releases")


def same_except_precip(key1, key2):
    """True if two run keys differ at most in PRECIP_KEYS."""
    def strip(k):
        return {n: v for n, v in (k or {}).items() if n not in PRECIP_KEYS}
    return strip(key1) == strip(key2)


def flow_end_target(end, after_days):
    """End of the flow window a run asks for: 12:00 UTC, after_days after the last PRISM day."""
    return dt.datetime.combine(end + dt.timedelta(days=after_days), dt.time(12), tzinfo=dt.timezone.utc)


def flow_stale(saved_end, target, now, slack_h=6.0):
    """True if a saved flow record (or skip) ending at saved_end was cut short of target by more than slack_h
    hours of data that now exists. Runs before the target time are capped at the time of the run."""
    return (min(target, now) - saved_end).total_seconds() / 3600 > slack_h


def load_cached(vdir, site_no, key, target=None, now=None):
    """(result, basin, None) or (None, skip reason, gauge) from an earlier run with the same settings, else None.

    Results saved with other PRECIP_KEYS are returned too, with a run_key that differs from key, so the
    caller can recompute their precipitation (update_precip); skips caused by precipitation are not reused
    then. Results whose flow window was cut short (run before target) are not reused once more flow exists.
    """
    path = vdir / f"runoff_{site_no}.json"
    if not path.exists():
        return None
    cached = json.loads(path.read_text())
    if not same_except_precip(cached.get("run_key"), key):
        return None
    if target is not None:
        saved = (cached.get("flow_window_utc") or {}).get("end") or cached.get("checked_utc")
        if not saved or flow_stale(dt.datetime.fromisoformat(saved), target, now or dt.datetime.now(dt.timezone.utc)):
            return None  # (skips saved before checked_utc was recorded can't be dated: check again)
    if cached.get("status") and cached["status"] != "ok":
        if cached.get("stage") == "precip" and cached.get("run_key") != key:
            return None
        return None, cached["status"], cached.get("gauge")
    basin_file = vdir / f"runoff_{site_no}_basin.geojson"
    if not basin_file.exists():
        return None
    return cached, gpd.read_file(basin_file), None


# ---------- main ----------

def main():
    from observations import RateLimited, nid_dams, usgs_discharge_gauges, usgs_site, usgs_sites

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="folder prism_watershed.py wrote to")
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--gauge", nargs="+", help="USGS site number(s), e.g. 09342500 09361500")
    which.add_argument("--all-gauges", action="store_true",
                       help="every USGS stream gauge in the study basin with 15-minute discharge for the window")
    ap.add_argument("--boundary", help="study basin polygon (default: the wbd_huc*.geojson in --out)")
    ap.add_argument("--start", help="first PRISM day (default: first day in the cube)")
    ap.add_argument("--end", help="last PRISM day (default: last day in the cube)")
    ap.add_argument("--after-days", type=int, default=7, help="days of flow after --end to include (default 7)")
    ap.add_argument("--max-gap-hours", type=float, default=12.0, help="longest discharge gap to interpolate (default 12)")
    ap.add_argument("--all-snow-c", type=float, default=0.0,
                    help="daily mean temperature at or below which precipitation is all snow (default 0 °C)")
    ap.add_argument("--all-rain-c", type=float, default=3.0,
                    help="daily mean temperature at or above which precipitation is all rain (default 3 °C)")
    ap.add_argument("--max-missing-pct", type=float, default=1.0,
                    help="share of a gauge basin allowed outside PRISM coverage (e.g. Canada), filled with the basin mean (default 1)")
    ap.add_argument("--minor-mm", type=float, default=1.0, help="upstream dam storage depth for 'minor' (default 1 mm)")
    ap.add_argument("--regulated-mm", type=float, default=10.0,
                    help="upstream dam storage depth for 'regulated' (default 10 mm)")
    ap.add_argument("--max-wait-min", type=float, default=15,
                    help="wait out USGS rate-limit pauses up to this long before stopping (default 15)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch every gauge instead of reusing results saved by an earlier run with the same settings")
    ap.add_argument("--res", default="800m", choices=["800m", "4km"])
    a = ap.parse_args()
    import observations
    observations.MAX_WAIT_S = a.max_wait_min * 60

    out = Path(a.out)
    nc = out / f"ppt_{a.res}_daily_clip.nc"
    if not nc.exists():
        sys.exit(f"No cube at {nc}. Run prism_watershed.py with --out {a.out} first.")
    study = load_basin(out, a.boundary)
    study_poly = study.geometry.iloc[0]
    vdir = out / "validation"
    vdir.mkdir(exist_ok=True)
    gpkg = vdir / "validation.gpkg"

    with xr.open_dataset(nc) as ds:
        have = ds.time.values.astype("datetime64[D]").astype(object)
        start = dt.date.fromisoformat(a.start) if a.start else min(have)
        end = dt.date.fromisoformat(a.end) if a.end else max(have)
        days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        missing = [d for d in days if d not in set(have)]
        if missing:
            sys.exit(f"Cube has no data for {', '.join(map(str, missing))}.")
        grids = []
        for d in days:
            grid, _ = prism_day_files(out / "extracted", "ppt", a.res, d)
            if grid is None:
                sys.exit(f"No extracted PRISM grid for {d} in {out / 'extracted'}; run prism_watershed.py for it.")
            grids.append((d, grid))
        tmean = {d: prism_day_files(out / "extracted", "tmean", a.res, d)[0] for d in days}
        if None in tmean.values():
            tmean = None
            print("No PRISM tmean grids for every day in extracted/; rain-only ratios skipped "
                  "(run prism_watershed.py --var tmean for the window).")
        rain_snow = [a.all_snow_c, a.all_rain_c] if tmean else None
        tmean_releases = []
        tnc = out / f"tmean_{a.res}_daily_clip.nc"
        if tmean and tnc.exists():
            with xr.open_dataset(tnc) as tds:
                if "release" in tds:
                    tmean_releases = [int(v) for v in tds.release.sel(time=slice(str(start), str(end))).values]

        releases = ([int(v) for v in ds.release.sel(time=slice(str(start), str(end))).values]
                    if "release" in ds else [])
        bbox = tuple(study.total_bounds)
        key = window_key(days, a, releases, rain_snow, tmean_releases)
        target = flow_end_target(end, a.after_days)
        stopped = None
        dam_cache = {}

        def dams_for(bounds):
            """NID dams covering bounds, widening the cached query when a gauge basin reaches past it."""
            w, s, e, n = dam_cache["bbox"]
            if bounds[0] < w or bounds[1] < s or bounds[2] > e or bounds[3] > n:
                wider = (min(w, bounds[0]), min(s, bounds[1]), max(e, bounds[2]), max(n, bounds[3]))
                dam_cache.update(bbox=wider, dams=nid_dams(wider))
            return dam_cache["dams"]
        try:
            dam_cache.update(bbox=bbox, dams=nid_dams(bbox))
            dams = [d for d in dam_cache["dams"] if study_poly.contains(Point(d["lon"], d["lat"]))]
            meta = usgs_sites(bbox)
            if a.all_gauges:
                flow_start = str(start - dt.timedelta(days=1))
                flow_end = str(min(end + dt.timedelta(days=a.after_days), dt.date.today() - dt.timedelta(days=1)))
                sites = usgs_discharge_gauges(bbox, flow_start, flow_end)
                print(f"{len(sites)} gauges with 15-minute discharge in the study area's bounding box; "
                      f"{len(dams)} NID dams in the study basin")
            else:
                sites = a.gauge
        except RateLimited as e:
            sys.exit(str(e))

        rows, basins = [], []
        for i, site_no in enumerate(sites):
            cached = None if a.refresh else load_cached(vdir, site_no, key, target)
            try:
                if cached and cached[0] is None:
                    raise GaugeSkipped(cached[1], cached[2])
                if cached:
                    result, basin = cached[0], cached[1]
                    if result["run_key"] != key:
                        result = update_precip(result, basin, grids, a, tmean)
                        result["run_key"] = key
                        result["flow_complete"] = dt.datetime.fromisoformat(result["flow_window_utc"]["end"]) >= target
                        (vdir / f"runoff_{site_no}.json").write_text(json.dumps(result, indent=2))
                        cached = "precipitation recomputed (new PRISM releases or rain/snow settings)"
                else:
                    try:
                        site = meta.get(site_no) or usgs_site(site_no)
                        result, basin = run_gauge(site_no, site, grids, study_poly, dams_for, a, vdir, tmean)
                    except RateLimited:
                        raise
                    except requests.RequestException as e:
                        raise GaugeSkipped(f"request failed: {e.__class__.__name__} "
                                           f"{getattr(e.response, 'status_code', '')}".strip(), meta.get(site_no))
                    except GaugeSkipped as e:
                        (vdir / f"runoff_{site_no}.json").write_text(json.dumps(
                            {"run_key": key, "status": str(e), "gauge": e.site, "stage": e.stage,
                             "checked_utc": dt.datetime.now(dt.timezone.utc).isoformat()}, indent=2))
                        raise
                    result["run_key"] = key
                    result["flow_complete"] = dt.datetime.fromisoformat(result["flow_window_utc"]["end"]) >= target
                    (vdir / f"runoff_{site_no}.json").write_text(json.dumps(result, indent=2))
            except GaugeSkipped as e:
                rows.append(summary_row(site_no, site=e.site, reason=str(e)))
                if not (a.all_gauges and str(e) == "gauge is outside the study basin"):
                    print(f"  {site_no} skipped: {e}")
                continue
            except RateLimited as e:
                stopped = (str(e), len(sites) - i)
                break
            row = summary_row(site_no, result)
            rows.append(row)
            basins.append(gpd.GeoDataFrame([row], geometry=[basin.to_crs(4269).geometry.iloc[0]], crs=4269))
            print(f"  {site_no} {row['name'][:42]:<42} {row['area_km2']:>8,.0f} km²  {row['precip_mm']:6.1f} mm  "
                  f"ratio {row['ratio_low']:.2f}–{row['ratio_high']:.2f}"
                  + (f" (rain-only {row['rain_ratio_low']:.2f}–{row['rain_ratio_high']:.2f}, {row['snow_pct']:.0f}% snow)"
                     if row.get("rain_ratio_low") is not None else
                     f" ({row['snow_pct']:.0f}% snow)" if "snow_pct" in row else "")
                  + f"  {row['regulation']}"
                  f" ({row['dam_effective_mm']:.1f} mm effective of {row['dam_storage_mm']:.1f} mm capacity,"
                  f" {row['storage_pct_of_precip']:.0f}% of precip)"
                  + (f"  [{cached}]" if isinstance(cached, str) else "  [saved]" if cached else "")
                  + ("" if row["flow_complete"] else "  [flow window not over yet]"))

    if stopped:  # keep the previous outputs; the gauges finished so far are saved and reused next run
        sys.exit(f"Stopped with {stopped[1]} gauges left: {stopped[0]} Rerun the same command to continue; "
                 "finished gauges are reused. Summary CSV and layers were left as they were.")
    this_run = list(rows)
    if a.all_gauges:  # keep only gauges in the study basin
        rows = [r for r in rows if r["status"] != "gauge is outside the study basin"]
    summary = vdir / "runoff_summary.csv"
    if not a.all_gauges and summary.exists():  # --gauge: replace these gauges' rows, keep the rest
        with open(summary, newline="") as f:
            done = {r["site"] for r in rows}
            rows = [r for r in csv.DictReader(f) if r["site"] not in done] + rows
    with open(summary, "w", newline="") as f:
        fields = list(dict.fromkeys(k for r in rows for k in r))
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if r["status"] == "ok"]
    # Layers: --all-gauges replaces them; --gauge replaces only the gauges run now (from this run's rows,
    # never from the CSV, whose values are text).
    if a.all_gauges:
        this_run = [r for r in this_run if r["status"] != "gauge is outside the study basin"]
    run_sites = [r["site"] for r in this_run]
    points = [r for r in this_run if r.get("lon") is not None]
    empty = gpd.GeoDataFrame({"site": pd.Series(dtype=str)}, geometry=gpd.GeoSeries([], crs=4269))
    gb = gpd.GeoDataFrame(pd.concat(basins, ignore_index=True), crs=4269) if basins else empty
    gp = (gpd.GeoDataFrame(points, geometry=gpd.points_from_xy([r["lon"] for r in points],
                                                               [r["lat"] for r in points]), crs=4269)
          if points else empty)
    if a.all_gauges:
        write_layer(gb, gpkg, "gauge_basins")
        write_layer(gp, gpkg, "gauges")
    else:
        upsert_layer(gb, gpkg, "gauge_basins", "site", run_sites)
        upsert_layer(gp, gpkg, "gauges", "site", run_sites)
    if dams:
        dg = pd.DataFrame(dams).drop(columns=["_af"], errors="ignore")
        dg["storage_af"] = dg["normal_storage_af"].fillna(dg["nid_storage_af"])
        write_layer(gpd.GeoDataFrame(dg, geometry=gpd.points_from_xy(dg.lon, dg.lat), crs=4269), gpkg, "dams")

    counts = pd.Series([r["regulation"] for r in ok]).value_counts().to_dict()
    print(f"PRISM days {start} to {end}: {len(ok)} gauges compared {counts}; "
          f"{len(rows) - len(ok)} skipped (reasons in runoff_summary.csv)")
    print(f"Outputs in {vdir.resolve()}")


if __name__ == "__main__":
    main()
