#!/usr/bin/env python3
"""
Compare PRISM daily precipitation with gauge observations in and around a basin.

Stations and daily totals come from RCC-ACIS (COOP, CoCoRaHS, ASOS/first-order,
SNOTEL). PRISM values are sampled from the full national grids in <out>/extracted/,
so stations in the buffer outside the basin are compared too.

Day alignment: a PRISM day ends at 12:00 UTC on its date. Each station day ends at
its observation hour (local standard time), so it is matched to the PRISM day whose
end is closest. Morning observers (CoCoRaHS ~7 AM) line up within a few hours;
midnight observers (SNOTEL, airports) are matched to the next PRISM day, about
5 hours apart. Station days more than --max-offset hours from any PRISM day end are
kept but left out of the statistics.

In-sample flag: PRISM lists the stations it used each day in *.stn.csv. A gauge
within --match-km of a listed station is marked in_sample; comparisons against those
gauges are optimistic because PRISM was fitted to them.

Outputs in <out>/validation/ (each CSV and JSON also as <name>_<start>_<end>.<ext>, so a folder keeps
results for several windows; the GeoPackage layers hold the latest run):
    validation.gpkg   layers 'stations' (event totals) and 'station_days' (per day,
                      with UTC start/end fields for the QGIS Temporal Controller),
                      each with a default QGIS style
    stations.csv, station_days.csv, stations_summary.json

Usage examples:
    python validate_stations.py --out data/huc14080104
    python validate_stations.py --out data/huc1408 --start 2026-10-20 --end 2026-10-27 --buffer-km 20
"""
import argparse
import csv
import datetime as dt
import sqlite3
from collections import Counter
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from shapely.geometry import Point

import config
import manifest
import runtime
from runtime import die
from storm_total import prism_day_files, utm_crs

INCH_MM = 25.4
STYLE_DIR = Path(__file__).resolve().parents[1] / "styles"
# GHCN-Daily id prefixes (3 characters) to network names.
GHCN_NETWORKS = {"USC": "COOP", "US1": "CoCoRaHS", "USW": "ASOS/first-order", "USS": "SNOTEL",
                 "USR": "RAWS"}


# ---------- parsing and alignment (pure functions) ----------

def parse_value(raw):
    """ACIS daily value -> (mm or None, kind). Kinds: value, trace, missing, subsequent, accumulated."""
    if raw in ("M", "", None):
        return None, "missing"
    if raw == "T":
        return 0.0, "trace"
    if raw == "S":
        return None, "subsequent"
    if raw.endswith("A"):
        return float(raw[:-1]) * INCH_MM, "accumulated"
    try:
        return float(raw) * INCH_MM, "value"
    except ValueError:
        return None, "missing"


def station_ids(sids):
    """(GHCN id or '', network name) from ACIS 'sids' entries like 'US1COLP0067 6'."""
    ghcn = next((s.split()[0] for s in sids if s.split()[-1] == "6"), "")
    if ghcn[:3] in GHCN_NETWORKS:
        return ghcn, GHCN_NETWORKS[ghcn[:3]]
    types = {s.split()[-1] for s in sids}
    if "10" in types:
        return ghcn, "CoCoRaHS"
    if "2" in types:
        return ghcn, "COOP"
    return ghcn, f"GHCN:{ghcn[:3]}" if ghcn else "other"


def prism_day_for(obs_date, hour, tzo):
    """Match a station day to a PRISM day.

    obs_date: date the observation was reported for; hour: hour (1-24, local standard time)
    the 24-hour period ended, 24 meaning midnight; tzo: UTC offset in hours.
    Returns (prism_date, offset_hours); offset = station end minus PRISM end.
    """
    end_utc = dt.datetime.combine(obs_date, dt.time()) + dt.timedelta(hours=hour - tzo)
    base = (end_utc - dt.timedelta(hours=12)).date()
    best = None
    for p in (base, base + dt.timedelta(days=1)):
        offset = (end_utc - dt.datetime.combine(p, dt.time(12))).total_seconds() / 3600
        if best is None or abs(offset) < abs(best[1]):
            best = (p, offset)
    return best


def station_days(acis_station, sdate, network):
    """Per-day records for one ACIS station, in date order.

    ACIS gives the observation hour 1-24 (24 = midnight); 0 or -1 mean not recorded.
    Unrecorded hours take the station's usual recorded hour, else a network default
    (midnight for SNOTEL, 7 AM otherwise); hour_known marks which is which.
    The UTC offset comes from ACIS ('tzo'); if missing, it is estimated from longitude.
    """
    meta = acis_station["meta"]
    tzo = meta.get("tzo")
    if tzo is None:
        tzo = float(round(meta["ll"][0] / 15)) if meta.get("ll") else -7.0
    cells = [(c[0][0], c[0][1]) for c in acis_station["data"]]
    hours = Counter(h for raw, h in cells if h is not None and h > 0
                    and parse_value(raw)[1] in ("value", "trace", "accumulated"))
    usual = hours.most_common(1)[0][0] if hours else (24 if network == "SNOTEL" else 7)
    recs = []
    for i, (raw, hour) in enumerate(cells):
        obs_date = sdate + dt.timedelta(days=i)
        mm, kind = parse_value(raw)
        known = hour is not None and hour > 0 and kind not in ("missing", "subsequent")
        pday, offset = prism_day_for(obs_date, hour if known else usual, tzo)
        recs.append({"obs_date": obs_date, "obs_hour": hour if known else usual, "hour_known": known,
                     "prism_day": pday, "offset_h": offset, "obs_mm": mm, "kind": kind})
    return recs


def event_total(recs, days):
    """Station total over the PRISM days in `days` -> (mm or None, reason).

    Accumulated values ('S' days followed by an 'A' total) count only when the whole
    accumulation lies inside the window.
    """
    idx = [i for i, r in enumerate(recs) if r["prism_day"] in days]
    if sorted({recs[i]["prism_day"] for i in idx}) != sorted(days) or len(idx) != len(days):
        hours = {recs[i]["obs_hour"] for i in idx if recs[i]["hour_known"]}
        if len(hours) > 1 and len(idx) >= len(days) - 1:
            # e.g. a 7 AM observer switching to midnight: two readings land on one PRISM day, none on another
            return None, "observation time changed in the window"
        return None, "days missing from record"
    first = idx[0]
    if (recs[first]["kind"] in ("subsequent", "accumulated") and first > 0
            and recs[first - 1]["kind"] == "subsequent"):
        return None, "accumulation starts before window"
    total, pending = 0.0, False
    for i in idx:
        kind = recs[i]["kind"]
        if kind == "missing":
            return None, "missing day"
        if kind == "subsequent":
            pending = True
            continue
        total += recs[i]["obs_mm"]
        pending = False
    if pending:
        return None, "accumulation ends after window"
    return total, "ok"


def stats(pred, obs):
    """Agreement statistics for paired PRISM (pred) and observed values in mm."""
    p, o = np.asarray(pred, float), np.asarray(obs, float)
    n = len(p)
    if n == 0:
        return {"n": 0}
    d = p - o
    out = {"n": n, "bias_mm": float(d.mean()), "mae_mm": float(np.abs(d).mean()),
           "rmse_mm": float(np.sqrt((d ** 2).mean())), "mean_obs_mm": float(o.mean()),
           "mean_prism_mm": float(p.mean())}
    out["pbias_pct"] = float(d.sum() / o.sum() * 100) if o.sum() else None
    out["r"] = float(np.corrcoef(p, o)[0, 1]) if n > 2 and p.std() and o.std() else None
    return out


# ---------- PRISM grids and station lists on disk ----------

def sample_grid(path, lonlat):
    """Grid values (mm, NaN for nodata) at (lon, lat) points; grid is NAD83 lat/lon."""
    with rasterio.open(path) as src:
        vals = np.array([v[0] for v in src.sample(lonlat)], dtype=float)
        if src.nodata is not None:
            vals[vals == src.nodata] = np.nan
    return vals


def prism_input_flags(stn_csv, lonlat, match_km):
    """True for each point within match_km of a station PRISM used that day."""
    with open(stn_csv) as f:
        rows = list(csv.DictReader(f.readlines()[1:]))
    if not rows:
        return np.zeros(len(lonlat), bool)
    slon = np.array([float(r["Longitude"]) for r in rows])
    slat = np.array([float(r["Latitude"]) for r in rows])
    flags = []
    for lon, lat in lonlat:
        dx = (slon - lon) * 111.32 * np.cos(np.radians(lat))
        dy = (slat - lat) * 110.57
        flags.append(bool(np.min(np.hypot(dx, dy)) <= match_km))
    return np.array(flags)


# ---------- GeoPackage styles ----------

def write_default_style(gpkg, layer, qml_path):
    """Store a QML style in the GeoPackage's layer_styles table as the layer's default."""
    qml = Path(qml_path).read_text()
    con = sqlite3.connect(gpkg)
    row = con.execute("SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?", (layer,)).fetchone()
    geom_col = row[0] if row else "geom"
    con.execute("""CREATE TABLE IF NOT EXISTS layer_styles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, f_table_catalog TEXT(256), f_table_schema TEXT(256),
        f_table_name TEXT(256), f_geometry_column TEXT(256), styleName TEXT(30), styleQML TEXT,
        styleSLD TEXT, useAsDefault BOOLEAN, description TEXT, owner TEXT(30), ui TEXT(30),
        update_time DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))""")
    con.execute("""INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier)
                   VALUES ('layer_styles', 'attributes', 'layer_styles')""")
    con.execute("DELETE FROM layer_styles WHERE f_table_name = ?", (layer,))
    con.execute("""INSERT INTO layer_styles (f_table_catalog, f_table_schema, f_table_name,
                   f_geometry_column, styleName, styleQML, styleSLD, useAsDefault, description, owner, update_time)
                   VALUES ('', '', ?, ?, ?, ?, '', 1, 'PRISM Storm Analysis default', '',
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
                (layer, geom_col, layer, qml))
    con.commit()
    con.close()


def write_layer(gdf, gpkg, layer, window=None, style=None):
    """Write a GeoPackage layer with its default QGIS style (styles/<layer>.qml). With window=(start, end),
    also write it as <layer>_<YYYYMMDD>_<YYYYMMDD> (same style), so a folder keeps every storm's layers:
    the plain name holds the latest run, the window-named one stays for that window. style names the .qml
    when layer is itself a window-named copy."""
    qml = STYLE_DIR / f"{style or layer}.qml"
    for name in [layer] + ([f"{layer}_{runtime.span(*window)}"] if window else []):
        gdf.to_file(gpkg, layer=name, driver="GPKG")
        if qml.exists():
            write_default_style(gpkg, name, qml)


# ---------- main ----------

def load_basin(out, boundary):
    if boundary:
        return gpd.read_file(boundary).to_crs(4269).dissolve()
    wbd = sorted(Path(out).glob("wbd_huc*.geojson")) or sorted(Path(out).glob("boundary.geojson"))
    if len(wbd) != 1:
        die("Pass --boundary: expected exactly one wbd_huc*.geojson (or boundary.geojson) in --out.")
    return gpd.read_file(wbd[0]).to_crs(4269).dissolve()


def cube_days(out, var, res):
    """PRISM days present in the cube, used as the default window."""
    import xarray as xr
    nc = Path(out) / f"{var}_{res}_daily_clip.nc"
    if not nc.exists():
        die(f"No cube at {nc}. Run prism_watershed.py with --out {out} first.")
    with xr.open_dataset(nc) as ds:
        t = ds.time.values.astype("datetime64[D]").astype(object)
    return min(t), max(t)


def main():
    from observations import acis_daily_precip

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="folder prism_watershed.py wrote to")
    ap.add_argument("--start", help="first PRISM day (default: first day in the cube)")
    ap.add_argument("--end", help="last PRISM day (default: last day in the cube)")
    ap.add_argument("--boundary", help="basin polygon (default: the wbd_huc*.geojson in --out)")
    ap.add_argument("--buffer-km", type=float, default=10.0, help="also compare stations this far outside the basin (default 10)")
    ap.add_argument("--max-offset", type=float, default=6.0,
                    help="hours between station and PRISM day ends allowed in the statistics (default 6)")
    ap.add_argument("--match-km", type=float, default=0.5, help="distance to a PRISM input station for in_sample (default 0.5)")
    ap.add_argument("--res", default="800m", choices=["800m", "4km"])
    runtime.add_common_args(ap)
    a = config.parse_args(ap)
    run = manifest.start_run("validate_stations.py", a)
    runtime.attach(a.out, run, a)

    out = Path(a.out)
    c0, c1 = cube_days(out, "ppt", a.res)
    start = dt.date.fromisoformat(a.start) if a.start else c0
    end = dt.date.fromisoformat(a.end) if a.end else c1
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]

    grids = {}
    for d in days:
        g, s = prism_day_files(out / "extracted", "ppt", a.res, d)
        if g is None:
            die(f"No extracted PRISM grid for {d} in {out / 'extracted'}; run prism_watershed.py for it.")
        grids[d] = (g, s)

    basin = load_basin(out, a.boundary)
    utm = utm_crs(basin)
    zone = basin.to_crs(utm).buffer(a.buffer_km * 1000).to_crs(4269)
    w, s_, e, n = zone.total_bounds
    # Fetch a day either side: midnight observers report a PRISM day under the previous date.
    acis = acis_daily_precip((w, s_, e, n), str(start - dt.timedelta(days=1)), str(end + dt.timedelta(days=1)))
    sdate = start - dt.timedelta(days=1)
    poly, zone_poly = basin.geometry.iloc[0], zone.iloc[0]
    acis = [st for st in acis if st["meta"].get("ll") and zone_poly.contains(Point(st["meta"]["ll"]))]
    if not acis:
        die("ACIS returned no stations within the basin and buffer.")
    lonlat = [tuple(st["meta"]["ll"]) for st in acis]
    print(f"{len(acis)} ACIS stations within the basin + {a.buffer_km:g} km")

    prism = {d: sample_grid(grids[d][0], lonlat) for d in days}
    used = {d: (prism_input_flags(grids[d][1], lonlat, a.match_km) if grids[d][1] else np.zeros(len(lonlat), bool))
            for d in days}

    st_rows, day_rows = [], []
    for k, st in enumerate(acis):
        meta = st["meta"]
        ghcn, network = station_ids(meta.get("sids", []))
        recs = station_days(st, sdate, network)
        inside = poly.contains(Point(meta["ll"]))
        in_sample = any(used[d][k] for d in days)
        window = [r for r in recs if r["prism_day"] in grids]
        hours = Counter(r["obs_hour"] for r in window if r["obs_hour"] is not None)
        offsets = [r["offset_h"] for r in window]
        aligned = bool(offsets) and max(abs(o) for o in offsets) <= a.max_offset
        for r in window:
            p = prism[r["prism_day"]][k]
            ok = r["kind"] in ("value", "trace") and abs(r["offset_h"]) <= a.max_offset and not np.isnan(p)
            day_rows.append({
                "station": meta["name"], "network": network, "ghcn_id": ghcn,
                "prism_day": r["prism_day"].isoformat(),
                "period_start": dt.datetime.combine(r["prism_day"] - dt.timedelta(days=1), dt.time(12)).isoformat() + "Z",
                "period_end": dt.datetime.combine(r["prism_day"], dt.time(12)).isoformat() + "Z",
                "obs_date": r["obs_date"].isoformat(), "obs_hour": r["obs_hour"], "hour_known": r["hour_known"], "offset_h": round(r["offset_h"], 1),
                "kind": r["kind"], "obs_mm": None if r["obs_mm"] is None else round(r["obs_mm"], 2),
                "prism_mm": None if np.isnan(p) else round(float(p), 2),
                "diff_mm": round(float(p) - r["obs_mm"], 2) if ok else None,
                "in_stats": ok, "in_sample": bool(used[r["prism_day"]][k]), "inside_basin": inside,
                "lon": meta["ll"][0], "lat": meta["ll"][1]})
        total, reason = event_total(recs, days)
        ptot = float(np.nansum([prism[d][k] for d in days])) if not any(np.isnan(prism[d][k]) for d in days) else None
        valid = total is not None and ptot is not None
        st_rows.append({
            "station": meta["name"], "network": network, "ghcn_id": ghcn, "state": meta.get("state"),
            "elev_m": round(meta["elev"] * 0.3048) if meta.get("elev") is not None else None,
            "obs_hour": hours.most_common(1)[0][0] if hours else None,
            "max_offset_h": round(max(offsets, key=abs), 1) if offsets else None,
            "aligned": aligned, "inside_basin": inside, "in_sample": in_sample,
            "obs_mm": None if total is None else round(total, 1),
            "prism_mm": None if ptot is None else round(ptot, 1),
            "diff_mm": round(ptot - total, 1) if valid else None,
            "ratio": round(ptot / total, 3) if valid and total > 0 else None,
            "status": (reason if total is None else "no PRISM value" if ptot is None
                       else "ok" if aligned else "day ends too far from PRISM day"),
            "in_stats": valid and aligned, "lon": meta["ll"][0], "lat": meta["ll"][1]})

    def subset(rows, **cond):
        return [r for r in rows if r["in_stats"] and all(r[k] == v for k, v in cond.items())]

    def ev(rows):  # PRISM vs observed, for event totals or days alike
        return stats([r["prism_mm"] for r in rows], [r["obs_mm"] for r in rows])

    networks = sorted({r["network"] for r in st_rows})
    summary = {
        "window": {"start": str(start), "end": str(end), "prism_days": len(days)},
        "stations": {"fetched": len(acis), "inside_basin": sum(r["inside_basin"] for r in st_rows),
                     "event_total_valid": sum(r["status"] == "ok" for r in st_rows),
                     "in_stats": sum(r["in_stats"] for r in st_rows),
                     "status_counts": dict(Counter(r["status"] for r in st_rows))},
        "settings": {"buffer_km": a.buffer_km, "max_offset_h": a.max_offset, "match_km": a.match_km},
        "event_totals": {
            "all": ev(subset(st_rows)), "inside_basin": ev(subset(st_rows, inside_basin=True)),
            "independent": ev(subset(st_rows, in_sample=False)), "in_sample": ev(subset(st_rows, in_sample=True)),
            "by_network": {nw: ev(subset(st_rows, network=nw)) for nw in networks}},
        "daily": {
            "all": ev(subset(day_rows)), "independent": ev(subset(day_rows, in_sample=False)),
            "in_sample": ev(subset(day_rows, in_sample=True)),
            "by_network": {nw: ev(subset(day_rows, network=nw)) for nw in networks}},
        "notes": ["diff and bias are PRISM minus observed.",
                  "in_sample stations were PRISM inputs; their agreement is optimistic.",
                  "Gauges undercatch, especially snow and wind-driven rain at high elevation."]}

    vdir = out / "validation"
    vdir.mkdir(exist_ok=True)
    gpkg = vdir / "validation.gpkg"
    for name, rows in (("stations", st_rows), ("station_days", day_rows)):
        with runtime.atomic_open(vdir / f"{name}.csv", "w", newline="") as f:
            wcsv = csv.DictWriter(f, fieldnames=rows[0].keys())
            wcsv.writeheader()
            wcsv.writerows(rows)
        runtime.keep_window_copy(vdir / f"{name}.csv", start, end)
        gdf = gpd.GeoDataFrame(rows, geometry=gpd.points_from_xy([r["lon"] for r in rows], [r["lat"] for r in rows]),
                               crs=4269)
        if name == "station_days":
            for c in ("period_start", "period_end"):
                gdf[c] = gpd.pd.to_datetime(gdf[c], utc=True)
        write_layer(gdf, gpkg, name, window=(start, end))
    runtime.write_json(vdir / "stations_summary.json", summary)
    runtime.keep_window_copy(vdir / "stations_summary.json", start, end)

    def line(label, s):
        if not s.get("n"):
            return f"  {label:<22} n=0"
        r = f"{s['r']:.2f}" if s.get("r") is not None else "–"
        pb = f"{s['pbias_pct']:+.0f}%" if s.get("pbias_pct") is not None else "–"
        return (f"  {label:<22} n={s['n']:<4} bias {s['bias_mm']:+6.1f} mm  MAE {s['mae_mm']:5.1f}  "
                f"RMSE {s['rmse_mm']:5.1f}  r {r}  PRISM vs gauges {pb}")

    print(f"PRISM days {start} to {end}; PRISM minus observed")
    print("Event totals:")
    for k in ("all", "inside_basin", "independent", "in_sample"):
        print(line(k, summary["event_totals"][k]))
    for nw, s in summary["event_totals"]["by_network"].items():
        print(line(nw, s))
    print("Daily values:")
    for k in ("all", "independent", "in_sample"):
        print(line(k, summary["daily"][k]))
    print(f"Excluded from event stats: {dict(Counter(r['status'] for r in st_rows if not r['in_stats']))}")
    print(f"Outputs in {vdir.resolve()} (open validation.gpkg in QGIS)")
    run.update(window={"start": str(start), "end": str(end)},
               outputs=manifest.files_entry([vdir / "stations.csv", vdir / "station_days.csv",
                                             vdir / "stations_summary.json", vdir / "validation.gpkg"], out))
    manifest.append_run(out, run)


if __name__ == "__main__":
    runtime.run_main(main)
