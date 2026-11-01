#!/usr/bin/env python3
"""
Check the rain/snow split against SNOTEL snow-water equivalent.

For every active SNOTEL station in the basin (plus --buffer-km), over the PRISM window:
    observed snow   SWE gained by the snow pillow: peak SWE in the window minus SWE before it
    station split   the station's own daily precipitation split by its own daily mean
                    temperature with storm_total.py's ramp (--all-snow-c, --all-rain-c);
                    tests the method without PRISM
    PRISM split     PRISM precipitation at the station's cell split by PRISM tmean at that cell;
                    what storm_total.py and validate_runoff.py use
Each is also given as a share of the station's (or PRISM's) precipitation.

Reading the numbers: observed snow is a lower bound on snow that fell, because rain and warm
spells melt some of it within the window (most early-season storms start on bare ground). Snow
gauges catch less than falls in wind, which pushes the observed share up. SNOTEL stations are
PRISM inputs, so PRISM precipitation at the station is close to the gauge by construction; the
independent test is the snow share.

Days: AWDB daily values are Pacific Standard Time calendar days with end-of-day SWE, so PRISM
days start..end are AWDB days start-1..end-1 (4 hours apart; see observations.py).

Needs prism_watershed.py runs for ppt and tmean over the window in --out (national grids in
<out>/extracted). Outputs in <out>/validation/: snotel.csv, snotel_summary.json, and layer
'snotel' in validation.gpkg.

Usage examples:
    python validate_snow.py --out data/huc1408
    python validate_snow.py --out data/huc1408 --start 2026-10-20 --end 2026-10-27 --all-snow-c -1 --all-rain-c 3
"""
import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import Point

from storm_total import prism_day_files, snow_fraction
from validate_stations import cube_days, load_basin, sample_grid, write_layer

INCH_MM = 25.4


def f_to_c(f):
    return (f - 32) * 5 / 9


def snotel_window(series, start, end, all_snow_c=0.0, all_rain_c=3.0):
    """Window totals for one SNOTEL station; raises ValueError with the reason when data are missing.

    series: {element: {'YYYY-MM-DD': value}} from observations.snotel_daily (inches, degF).
    PRISM days start..end are AWDB days start-1..end-1; SWE before the window is the end-of-day
    reading of start-2.
    """
    days = [start - dt.timedelta(days=1) + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    prcp = [series.get("PRCP", {}).get(str(d)) for d in days]
    if None in prcp:
        raise ValueError("daily precipitation missing on " + ", ".join(str(d) for d, p in zip(days, prcp) if p is None))
    wteq = series.get("WTEQ", {})
    before = wteq.get(str(days[0] - dt.timedelta(days=1)))
    swe = [wteq.get(str(d)) for d in days]
    if before is None or None in swe:
        raise ValueError("snow-water equivalent missing in the window")
    precip_mm = sum(prcp) * INCH_MM
    gain = max(0.0, (max(swe) - before) * INCH_MM)
    tavg = [series.get("TAVG", {}).get(str(d)) for d in days]
    ramp = None
    if None not in tavg:
        ramp = float(sum(p * INCH_MM * snow_fraction(f_to_c(t), all_snow_c, all_rain_c) for p, t in zip(prcp, tavg)))
    return {"precip_mm": precip_mm, "swe_before_mm": before * INCH_MM, "swe_gain_mm": gain,
            "swe_net_mm": (swe[-1] - before) * INCH_MM, "station_split_snow_mm": ramp,
            "mean_tavg_c": float(np.mean([f_to_c(t) for t in tavg])) if None not in tavg else None}


def prism_split(ppt_mm, tmean_c, all_snow_c=0.0, all_rain_c=3.0):
    """(precipitation, snow) in mm over the window at one cell from daily PRISM values."""
    ppt, t = np.asarray(ppt_mm, float), np.asarray(tmean_c, float)
    return float(ppt.sum()), float((ppt * snow_fraction(t, all_snow_c, all_rain_c)).sum())


def share(part, whole):
    return round(part / whole * 100, 1) if part is not None and whole else None


def summarize(rows, min_precip_mm):
    """Station-average snow shares and the PRISM-to-observed snow ratio, over stations with enough precipitation."""
    use = [r for r in rows if r["status"] == "ok" and r["precip_mm"] >= min_precip_mm]
    if not use:
        return {"stations": 0}

    def mean(key):
        vals = [r[key] for r in use if r.get(key) is not None]
        return round(float(np.mean(vals)), 1) if vals else None
    obs = sum(r["swe_gain_mm"] for r in use)
    prism = [r["prism_snow_mm"] for r in use if r.get("prism_snow_mm") is not None]
    return {"stations": len(use), "min_precip_mm": min_precip_mm,
            "observed_snow_pct": mean("observed_snow_pct"), "station_split_snow_pct": mean("station_split_snow_pct"),
            "prism_snow_pct": mean("prism_snow_pct"),
            "prism_over_observed_snow": round(sum(prism) / obs, 2) if obs and len(prism) == len(use) else None}


def main():
    from observations import snotel_daily, snotel_stations

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="folder prism_watershed.py wrote to")
    ap.add_argument("--start", help="first PRISM day (default: first day in the cube)")
    ap.add_argument("--end", help="last PRISM day (default: last day in the cube)")
    ap.add_argument("--boundary", help="basin polygon (default: the wbd_huc*.geojson in --out)")
    ap.add_argument("--buffer-km", type=float, default=10.0, help="also use stations this far outside the basin")
    ap.add_argument("--all-snow-c", type=float, default=0.0, help="all snow at or below this daily mean (default 0 °C)")
    ap.add_argument("--all-rain-c", type=float, default=3.0, help="all rain at or above this daily mean (default 3 °C)")
    ap.add_argument("--min-precip-mm", type=float, default=10.0,
                    help="leave stations with less precipitation out of the summary (default 10 mm)")
    ap.add_argument("--res", default="800m", choices=["800m", "4km"])
    a = ap.parse_args()

    out = Path(a.out)
    c0, c1 = cube_days(out, "ppt", a.res)
    start = dt.date.fromisoformat(a.start) if a.start else c0
    end = dt.date.fromisoformat(a.end) if a.end else c1
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    ppt_grids = [prism_day_files(out / "extracted", "ppt", a.res, d)[0] for d in days]
    if None in ppt_grids:
        sys.exit(f"No extracted PRISM ppt grid for every day in {out / 'extracted'}; run prism_watershed.py.")
    t_grids = [prism_day_files(out / "extracted", "tmean", a.res, d)[0] for d in days]
    if None in t_grids:
        t_grids = None
        print("No PRISM tmean grids for every day; PRISM split skipped (run prism_watershed.py --var tmean).")

    basin = load_basin(out, a.boundary)
    zone = basin.to_crs(basin.estimate_utm_crs()).buffer(a.buffer_km * 1000).to_crs(4269).union_all()
    study = basin.geometry.iloc[0]
    stations = [s for s in snotel_stations(tuple(zone.bounds)) if zone.contains(Point(s["lon"], s["lat"]))]
    if not stations:
        sys.exit("No active SNOTEL stations in the basin or buffer.")
    series = snotel_daily([s["triplet"] for s in stations], start - dt.timedelta(days=2), end - dt.timedelta(days=1))

    lonlat = [(s["lon"], s["lat"]) for s in stations]
    ppt = np.array([sample_grid(g, lonlat) for g in ppt_grids])  # (days, stations)
    tmean = np.array([sample_grid(g, lonlat) for g in t_grids]) if t_grids else None

    rows = []
    for i, s in enumerate(stations):
        row = {"triplet": s["triplet"], "name": s["name"], "elevation_m": s["elevation_m"],
               "inside_basin": study.contains(Point(s["lon"], s["lat"])), "lon": s["lon"], "lat": s["lat"]}
        try:
            w = snotel_window(series.get(s["triplet"], {}), start, end, a.all_snow_c, a.all_rain_c)
        except ValueError as e:
            rows.append({**row, "status": str(e)})
            continue
        p_ppt, p_snow = (prism_split(ppt[:, i], tmean[:, i], a.all_snow_c, a.all_rain_c) if tmean is not None
                         else (float(ppt[:, i].sum()), None))
        rows.append({**row, "status": "ok",
                     "precip_mm": round(w["precip_mm"], 1), "prism_precip_mm": round(p_ppt, 1),
                     "swe_before_mm": round(w["swe_before_mm"], 1), "swe_gain_mm": round(w["swe_gain_mm"], 1),
                     "swe_net_mm": round(w["swe_net_mm"], 1),
                     "station_split_snow_mm": None if w["station_split_snow_mm"] is None
                     else round(w["station_split_snow_mm"], 1),
                     "prism_snow_mm": None if p_snow is None else round(p_snow, 1),
                     "observed_snow_pct": share(w["swe_gain_mm"], w["precip_mm"]),
                     "station_split_snow_pct": share(w["station_split_snow_mm"], w["precip_mm"]),
                     "prism_snow_pct": share(p_snow, p_ppt),
                     "station_mean_tavg_c": None if w["mean_tavg_c"] is None else round(w["mean_tavg_c"], 1)})

    vdir = out / "validation"
    vdir.mkdir(exist_ok=True)
    with open(vdir / "snotel.csv", "w", newline="") as f:
        fields = list(dict.fromkeys(k for r in rows for k in r))
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    write_layer(gpd.GeoDataFrame(rows, geometry=[Point(r["lon"], r["lat"]) for r in rows], crs=4269),
                vdir / "validation.gpkg", "snotel")
    summary = {"window": {"start": str(start), "end": end.isoformat()},
               "ramp_c": [a.all_snow_c, a.all_rain_c], **summarize(rows, a.min_precip_mm)}
    (vdir / "snotel_summary.json").write_text(json.dumps(summary, indent=2))

    def fmt(v):
        return "   -" if v is None else f"{v:4.0f}"
    print(f"PRISM days {start} to {end}; snow = all at or below {a.all_snow_c:g} °C, all rain at or above "
          f"{a.all_rain_c:g} °C (daily mean)")
    print(f"{'SNOTEL station':<24} {'elev m':>6} {'precip':>7} {'SWE gain':>8}   snow % of precip: "
          "observed  station split  PRISM split")
    for r in sorted(rows, key=lambda r: -(r.get("elevation_m") or 0)):
        if r["status"] != "ok":
            print(f"{r['name'][:24]:<24} {r['elevation_m'] or 0:6.0f}  skipped: {r['status']}")
            continue
        print(f"{r['name'][:24]:<24} {r['elevation_m']:6.0f} {r['precip_mm']:6.0f}mm {r['swe_gain_mm']:6.0f}mm"
              f"                      {fmt(r['observed_snow_pct'])}           {fmt(r['station_split_snow_pct'])}"
              f"         {fmt(r['prism_snow_pct'])}" + ("" if r["inside_basin"] else "   (outside basin)"))
    if summary["stations"]:
        print(f"\nAverage over {summary['stations']} stations with >= {a.min_precip_mm:g} mm: observed "
              f"{summary['observed_snow_pct']}%, station split {summary['station_split_snow_pct']}%, PRISM split "
              f"{summary['prism_snow_pct']}%; PRISM snow / observed SWE gain = {summary['prism_over_observed_snow']}")
    print(f"Outputs in {vdir.resolve()}")


if __name__ == "__main__":
    main()
