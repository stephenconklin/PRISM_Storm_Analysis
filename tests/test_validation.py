"""Offline tests for station and runoff validation processing (no network)."""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point, box
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_stations import (event_total, parse_value, prism_day_for, prism_input_flags,  # noqa: E402
                               sample_grid, station_days, station_ids, stats, write_default_style)
from validate_runoff import (CFS_TO_M3S, M3_PER_ACRE_FT, basin_precip, flow_series,  # noqa: E402
                             regulation, runoff_volumes)

D = dt.date(2026, 10, 22)


def test_parse_value():
    assert parse_value("1.00") == (25.4, "value")
    assert parse_value("T") == (0.0, "trace")
    assert parse_value("M") == (None, "missing")
    assert parse_value("S") == (None, "subsequent")
    assert parse_value("2.00A") == (50.8, "accumulated")


def test_station_ids():
    assert station_ids(["US1COLP0067 6", "COLP0067 10"]) == ("US1COLP0067", "CoCoRaHS")
    assert station_ids(["USS0006M03S 6"]) == ("USS0006M03S", "SNOTEL")
    assert station_ids(["052433 2"])[1] == "COOP"


@pytest.mark.parametrize("hour, prism, offset", [
    (7, D, 2.0),                               # 7 AM MST = 14:00 UTC, 2 h after PRISM day D ends
    (24, D + dt.timedelta(days=1), -5.0),      # midnight MST = 07:00 UTC next day
    (18, D + dt.timedelta(days=1), -11.0),     # evening observer: poor match either way
])
def test_prism_day_for(hour, prism, offset):
    assert prism_day_for(D, hour, -7.0) == (prism, offset)


def acis(values, hours, tzo=-7.0):
    return {"meta": {"tzo": tzo}, "data": [[[v, h]] for v, h in zip(values, hours)]}


def test_station_days_unrecorded_hours():
    # CoCoRaHS: missing day and a reading with hour 0 take the usual 7 AM, not midnight.
    recs = station_days(acis(["0.10", "M", "0.30", "0.20"], [7, 0, 0, 7]), D, "CoCoRaHS")
    assert [r["prism_day"] for r in recs] == [D + dt.timedelta(days=i) for i in range(4)]
    assert [r["hour_known"] for r in recs] == [True, False, False, True]
    # SNOTEL reports hour 0 throughout: default to midnight, so each maps to the next PRISM day.
    snotel = station_days(acis(["0.10", "0.20"], [0, 0]), D, "SNOTEL")
    assert [r["prism_day"] for r in snotel] == [D + dt.timedelta(days=1), D + dt.timedelta(days=2)]


def test_event_total_rules():
    days = [D, D + dt.timedelta(days=1), D + dt.timedelta(days=2)]
    ok = station_days(acis(["0.00", "1.00", "T", "0.50"], [7] * 4), D, "COOP")
    assert event_total(ok, days) == (pytest.approx(25.4), "ok")
    missing = station_days(acis(["0.00", "M", "1.00"], [7] * 3), D, "COOP")
    assert event_total(missing, days)[1] == "missing day"
    accum_inside = station_days(acis(["S", "S", "3.00A", "0.0"], [7] * 4), D, "COOP")
    assert event_total(accum_inside, days) == (pytest.approx(76.2), "ok")
    accum_before = station_days(acis(["S", "S", "3.00A", "0.0"], [7] * 4), D - dt.timedelta(days=1), "COOP")
    assert event_total(accum_before, days)[1] == "accumulation starts before window"
    accum_after = station_days(acis(["0.10", "0.20", "S", "1.00A"], [7] * 4), D, "COOP")
    assert event_total(accum_after, days)[1] == "accumulation ends after window"


def test_stats():
    s = stats([12, 18, 30], [10, 20, 30])
    assert s["n"] == 3 and s["bias_mm"] == pytest.approx(0.0)
    assert s["mae_mm"] == pytest.approx(4 / 3) and s["pbias_pct"] == pytest.approx(0.0)
    assert stats([], [])["n"] == 0


def test_sample_grid_and_input_flags(tmp_path):
    tif = tmp_path / "g.tif"
    data = np.arange(100, dtype="float32").reshape(10, 10)
    data[0, 0] = -9999
    with rasterio.open(tif, "w", driver="GTiff", height=10, width=10, count=1, dtype="float32",
                       crs="EPSG:4269", transform=from_origin(-108.0, 38.0, 0.1, 0.1), nodata=-9999) as dst:
        dst.write(data, 1)
    vals = sample_grid(tif, [(-107.95, 37.95), (-107.55, 37.45)])
    assert np.isnan(vals[0]) and vals[1] == 54
    stn = tmp_path / "x.stn.csv"
    stn.write_text("header line\nStation,Name,Longitude,Latitude,Elevation(m),Network,station_id\n"
                   '"A","A",-107.5500,37.4500,2000,"COOP","1"\n')
    assert list(prism_input_flags(stn, [(-107.551, 37.451), (-107.0, 37.0)], 0.5)) == [True, False]


def test_write_default_style(tmp_path):
    gpkg = tmp_path / "v.gpkg"
    gpd.GeoDataFrame({"a": [1]}, geometry=[Point(0, 0)], crs=4269).to_file(gpkg, layer="stations")
    qml = tmp_path / "s.qml"
    qml.write_text("<qgis></qgis>")
    write_default_style(gpkg, "stations", qml)
    write_default_style(gpkg, "stations", qml)  # replaces, not duplicates
    con = sqlite3.connect(gpkg)
    assert con.execute("SELECT f_table_name, useAsDefault, styleQML FROM layer_styles").fetchall() == \
        [("stations", 1, "<qgis></qgis>")]


def test_flow_series_gap_and_volumes():
    t0 = pd.Timestamp("2026-10-21T12:00Z")
    times = pd.date_range(t0, periods=9, freq="15min")
    vals = [100, 100, 200, None, None, 200, 100, 100, 100]  # 45-minute gap (two missing readings)
    raw = [(t.isoformat(), None if v is None else str(v), None, "Provisional") for t, v in zip(times, vals)]
    q, interp, info = flow_series(raw, times[0], times[-1])
    assert info["longest_gap_h"] == pytest.approx(0.75) and info["interpolated_h"] == pytest.approx(0.5)
    assert q.iloc[3] == pytest.approx(200) and interp.iloc[3]
    vols, base_c, base_l = runoff_volumes(q)
    # Trapezoid volume of 100 cfs baseline over 2 h, plus the 100 cfs bump.
    expected_total = (100 * 2 + 100 * 1.0) * 3600 * CFS_TO_M3S
    assert vols["total_flow_m3"] == pytest.approx(expected_total)
    assert vols["runoff_constant_m3"] == pytest.approx(100 * 1.0 * 3600 * CFS_TO_M3S)
    assert vols["runoff_line_m3"] == pytest.approx(vols["runoff_constant_m3"])  # start and end flow equal


def national_grid(path, values):
    """A 2x2, 0.1-degree 'national' grid in NAD83 with nodata -9999."""
    with rasterio.open(path, "w", driver="GTiff", height=2, width=2, count=1, dtype="float32", crs="EPSG:4269",
                       transform=from_origin(-108.0, 38.0, 0.1, 0.1), nodata=-9999) as dst:
        dst.write(np.array(values, dtype="float32"), 1)
    return path


def test_basin_precip_from_national_grid(tmp_path):
    from storm_total import cell_area_m2
    basin = gpd.GeoDataFrame(geometry=[box(-108.0, 37.8, -107.8, 38.0)], crs=4269)
    day = dt.date(2026, 10, 22)
    area = cell_area_m2(from_origin(-108.0, 38.0, 0.1, 0.1), 2)
    rows, missing, area_m2 = basin_precip([(day, national_grid(tmp_path / "a.tif", [[10, 20], [30, 40]]))], basin)
    assert missing == 0 and area_m2 == pytest.approx(2 * area.sum())
    expected = (15 * area[0] + 35 * area[1]) / area.sum()  # area-weighted: rows differ slightly in area
    assert rows[0]["depth_mm"] == pytest.approx(expected) and rows[0]["volume_m3"] == pytest.approx(expected / 1000 * area_m2)
    # One cell outside PRISM coverage (e.g. Canada): filled with the basin mean if allowed, else an error.
    gap = national_grid(tmp_path / "b.tif", [[10, 20], [30, -9999]])
    rows, missing, _ = basin_precip([(day, gap)], basin, max_missing_pct=30)
    assert missing == pytest.approx(area[1] / (2 * area.sum()) * 100)
    with pytest.raises(ValueError, match="outside PRISM coverage"):
        basin_precip([(day, gap)], basin, max_missing_pct=1)


def test_prism_coverage():
    from prism_watershed import prism_coverage
    basin = gpd.GeoDataFrame(geometry=[box(-108.0, 37.8, -107.8, 38.0)], crs=4269)
    arr = np.ma.masked_equal(np.array([[10, 20], [30, -9999]], float), -9999)
    pct, missing_km2, basin_km2 = prism_coverage(arr, from_origin(-108.0, 38.0, 0.1, 0.1), basin)
    assert 24 < pct < 26 and missing_km2 < basin_km2 / 3


def test_regulation_classes():
    basin = gpd.GeoDataFrame(geometry=[box(-108.0, 37.0, -107.0, 38.0)], crs=4269)
    area = 1e10  # 10,000 km²
    pond = {"name": "Pond", "nidid": "X1", "normal_storage_af": 50, "nid_storage_af": 80, "lon": -107.5, "lat": 37.5}
    big = {"name": "Big", "nidid": "X2", "normal_storage_af": None, "nid_storage_af": 200000, "lon": -107.2, "lat": 37.2}
    outside = {"name": "Elsewhere", "nidid": "X3", "normal_storage_af": 900000, "nid_storage_af": 900000,
               "lon": -106.0, "lat": 37.5}
    r = regulation(basin, [pond, outside], area)
    assert r["n_dams"] == 1 and r["class"] == "unregulated"
    assert r["storage_mm"] == pytest.approx(50 * M3_PER_ACRE_FT / area * 1000)
    r = regulation(basin, [pond, big, outside], area)
    assert r["n_dams"] == 2 and r["storage_af"] == 200050 and r["class"] == "regulated"  # ~24.7 mm
    assert r["largest"][0]["name"] == "Big"
    assert regulation(basin, [big], area, regulated_mm=30)["class"] == "minor"



def test_regulation_caps_storage_by_dam_drainage_area():
    """A big reservoir on a small side creek (Electra Lake above Durango) can only hold what falls on its own
    catchment; a main-stem reservoir with a large drainage area keeps its capacity."""
    from validate_runoff import KM2_PER_MI2, regulation, regulation_from
    basin = gpd.GeoDataFrame(geometry=[box(-108.0, 37.0, -107.0, 38.0)], crs=4269)
    area = 1765e6  # Animas at Durango
    side = {"name": "Terminal", "nidid": "CO00895", "normal_storage_af": 22000, "nid_storage_af": 22500,
            "drainage_area_mi2": 26, "lon": -107.8, "lat": 37.5}
    r = regulation(basin, [side], area)  # no precipitation given: capacity, as before
    assert r["class"] == "regulated" and r["effective_mm"] == pytest.approx(r["storage_mm"])
    r = regulation(basin, [side], area, precip_mm=125)
    inflow_af = 0.125 * 26 * KM2_PER_MI2 * 1e6 / M3_PER_ACRE_FT  # ~6,800 acre-ft
    assert r["effective_af"] == pytest.approx(inflow_af) and r["class"] == "minor"
    assert r["storage_af"] == 22000 and r["largest"][0]["effective_af"] == round(inflow_af)
    main = {"name": "Navajo", "nidid": "NM00120", "normal_storage_af": 1708600, "nid_storage_af": 1708600,
            "drainage_area_mi2": 3206, "lon": -107.5, "lat": 37.2}
    nodata = {"name": "Unknown", "nidid": "X9", "normal_storage_af": 900, "nid_storage_af": 900,
              "drainage_area_mi2": None, "lon": -107.4, "lat": 37.3}
    r = regulation(basin, [main, nodata], 8379e6, precip_mm=137)
    assert r["class"] == "regulated" and r["n_no_drainage_area"] == 1
    assert r["dams"][1]["effective_af"] == 900  # no drainage area: full capacity
    # Recomputed offline from the saved list when the precipitation changes.
    assert regulation_from(r["dams"], 8379e6, precip_mm=0.0)["effective_af"] == 900


def test_dedupe_dams():
    from observations import dedupe_dams
    d = lambda name, af, lon, lat: {"name": name, "normal_storage_af": af, "nid_storage_af": af, "lon": lon, "lat": lat}
    dams = [d("Narraguinnep - Main Dam", 18960, -108.60, 37.50), d("Narraguinnep - Dam 2", 18960, -108.58, 37.52),
            d("Bauer Lake #2 - West Dam", 1532, -108.3, 37.3), d("Bauer Lake #2 - Main Dam", 1532, -108.3, 37.3),
            d("Other Pond", 1532, -107.0, 36.5), d("Unnamed", None, -107.0, 36.5)]
    assert [x["name"] for x in dedupe_dams(dams)] == [
        "Narraguinnep - Main Dam", "Bauer Lake #2 - West Dam", "Other Pond", "Unnamed"]


def test_zip_cache_name_and_lookup(tmp_path):
    from prism_watershed import download_day, zip_name
    assert zip_name("ppt", "800m", "us", D) == "prism_ppt_us_30s_20261022.zip"  # PRISM's own COG name
    names = {zip_name("ppt", r, "us", D, f) for r in ("800m", "4km") for f in ("", "nc")}
    assert len(names) == 4
    (tmp_path / "prism_ppt_us_30s_20261022.zip").write_bytes(b"x")
    # The cached 800 m zip is found without a network call (session=None)...
    assert download_day("ppt", "800m", "us", D, "", tmp_path, None).name == "prism_ppt_us_30s_20261022.zip"
    # ...and is not mistaken for the 4 km one, which would need a download.
    with pytest.raises(AttributeError):
        download_day("ppt", "4km", "us", D, "", tmp_path, None)


def test_prism_day_files_matches_resolution_and_format(tmp_path):
    from validate_stations import prism_day_files
    for folder, fname in (("prism_ppt_us_25m_20261022", "prism_ppt_us_25m_20261022.tif"),
                          ("prism_ppt_us_30s_20261022_nc", "prism_ppt_us_30s_20261022.nc"),
                          ("prism_ppt_us_30s_20261022", "prism_ppt_us_30s_20261022.tif")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / fname).write_bytes(b"x")
    (tmp_path / "prism_ppt_us_30s_20261022" / "prism_ppt_us_30s_20261022.stn.csv").write_text("x")
    grid, stn = prism_day_files(tmp_path, "ppt", "800m", D)
    assert grid.name == "prism_ppt_us_30s_20261022.tif" and stn is not None
    assert prism_day_files(tmp_path, "ppt", "4km", D)[0].name == "prism_ppt_us_25m_20261022.tif"
    (tmp_path / "prism_ppt_us_30s_20261022" / "prism_ppt_us_30s_20261022.tif").unlink()
    assert prism_day_files(tmp_path, "ppt", "800m", D)[0].suffix == ".nc"


def test_missing_tzo_from_longitude():
    st = {"meta": {"ll": [-80.0, 35.0]}, "data": [[["0.50", 7]]]}  # Eastern: 7 AM EST = 12:00 UTC
    rec = station_days(st, D, "CoCoRaHS")[0]
    assert rec["prism_day"] == D and rec["offset_h"] == 0.0


def test_basin_share_inside():
    from validate_runoff import basin_share_inside
    study = box(-108.0, 37.0, -107.0, 38.0)
    inside = gpd.GeoDataFrame(geometry=[box(-107.8, 37.2, -107.6, 37.4)], crs=4269)
    straddling = gpd.GeoDataFrame(geometry=[box(-107.1, 37.2, -106.7, 37.4)], crs=4269)
    assert basin_share_inside(inside, study) == pytest.approx(1.0)
    assert 0.2 < basin_share_inside(straddling, study) < 0.3


def test_zone_totals_add_up():
    from storm_total import zone_totals
    lat = np.array([37.95, 37.85]); lon = np.array([-107.95, -107.85])
    t = np.array(["2026-10-22", "2026-10-23"], dtype="datetime64[ns]")
    ppt = np.array([[[10, 20], [30, np.nan]], [[5, 5], [5, np.nan]]], float)  # one cell without PRISM data
    ds = xr.Dataset({"ppt": (("time", "lat", "lon"), ppt), "cell_area": (("lat", "lon"), np.full((2, 2), 1e6))},
                    coords={"time": t, "lat": lat, "lon": lon})
    whole, west, east = zone_totals(ds, [box(-108.0, 37.8, -107.8, 38.0), box(-108.0, 37.8, -107.9, 38.0),
                                         box(-107.9, 37.8, -107.8, 38.0)], "2026-10-22", "2026-10-23")
    assert whole["volume_m3"] == pytest.approx((15 + 25 + 35) / 1000 * 1e6)
    assert whole["coverage_pct"] == pytest.approx(75) and whole["mean_mm"] == pytest.approx(25)
    assert west["volume_m3"] + east["volume_m3"] == pytest.approx(whole["volume_m3"])
    assert east["coverage_pct"] == pytest.approx(50) and east["max_mm"] == pytest.approx(25)


def test_subbasin_level_and_ranking():
    from subbasin_totals import pick_digits, subbasin_rows
    counts = {6: 1, 8: 8, 10: 117, 12: 900}
    assert pick_digits("1408", counts.get) == 10
    assert pick_digits("17110005", {10: 9, 12: 57}.get) == 12
    assert pick_digits("140801040501", {}.get) == 12
    units = [{"properties": {"huc10": "1", "name": "A"}}, {"properties": {"huc10": "2", "name": "B"}}]
    totals = [{"area_km2": 10, "coverage_pct": 100, "mean_mm": 10, "max_mm": 12, "volume_m3": 1e5},
              {"area_km2": 10, "coverage_pct": 100, "mean_mm": 30, "max_mm": 40, "volume_m3": 3e5}]
    rows = subbasin_rows(units, totals, 4e5, 10)
    assert [r["rank"] for r in rows] == [2, 1] and rows[1]["share_pct"] == 75.0 and rows[0]["level"] == "HUC10"


def test_runoff_cache_reused_across_releases(tmp_path):
    """A gauge saved with older PRISM releases keeps its flow; only precipitation and ratios are recomputed."""
    import json
    from types import SimpleNamespace
    from validate_runoff import load_cached, update_precip, window_key
    a = SimpleNamespace(after_days=7, max_gap_hours=12.0, minor_mm=1.0, regulated_mm=10.0, max_missing_pct=1.0,
                        all_snow_c=0.0, all_rain_c=3.0, res="800m")
    days = [D]
    old_key, new_key = window_key(days, a, [1]), window_key(days, a, [2])
    basin = gpd.GeoDataFrame(geometry=[box(-108.0, 37.8, -107.8, 38.0)], crs=4269)
    basin.to_file(tmp_path / "runoff_09342500_basin.geojson", driver="GeoJSON")
    flow = {"runoff_constant_m3": 1e5, "runoff_line_m3": 5e4, "total_flow_m3": 2e5}
    saved = {"gauge": {"site": "09342500"}, "flow": flow, "run_key": {k: v for k, v in old_key.items()
                                                                       if k != "releases"}}  # pre-release cache
    (tmp_path / "runoff_09342500.json").write_text(json.dumps(saved))

    result, cached_basin, _ = load_cached(tmp_path, "09342500", new_key)
    assert result["run_key"] != new_key
    assert load_cached(tmp_path, "09342500", {**new_key, "after_days": 10}) is None
    grid = national_grid(tmp_path / "g.tif", [[10, 10], [10, 10]])
    result = update_precip(result, cached_basin, [(D, grid)], a)
    assert result["precip"]["depth_mm"] == pytest.approx(10.0, rel=1e-4)
    assert result["runoff_ratio"]["constant_baseflow"] == pytest.approx(1e5 / result["precip"]["volume_m3"])
    assert result["rain"] is None  # no tmean grids

    # Adding the rain/snow split recomputes precipitation only: 1.5 °C everywhere is half snow.
    key3 = window_key(days, a, [2], [0.0, 3.0])
    result, cached_basin, _ = load_cached(tmp_path, "09342500", key3)
    tgrid = national_grid(tmp_path / "t.tif", [[1.5, 1.5], [1.5, 1.5]])
    result = update_precip(result, cached_basin, [(D, grid)], a, {D: tgrid})
    assert result["rain"]["depth_mm"] == pytest.approx(5.0, rel=1e-4) and result["rain"]["snow_pct"] == pytest.approx(50)
    assert result["runoff_ratio"]["rain_constant_baseflow"] == pytest.approx(1e5 / result["rain"]["volume_m3"])


def test_basin_precip_rain_by_cell(tmp_path):
    """Rain is split cell by cell: a cold cell's precipitation is snow, a warm cell's is rain."""
    from storm_total import cell_area_m2
    basin = gpd.GeoDataFrame(geometry=[box(-108.0, 37.8, -107.8, 38.0)], crs=4269)
    ppt = national_grid(tmp_path / "p.tif", [[10, 10], [20, 20]])
    tmean = national_grid(tmp_path / "t.tif", [[-5, -5], [10, 10]])  # top row all snow, bottom row all rain
    rows, _, _ = basin_precip([(D, ppt)], basin, tmean={D: tmean})
    area = cell_area_m2(from_origin(-108.0, 38.0, 0.1, 0.1), 2)
    assert rows[0]["rain_mm"] == pytest.approx(20 * area[1] / area.sum())
    assert rows[0]["depth_mm"] == pytest.approx((10 * area[0] + 20 * area[1]) / area.sum())


def test_snotel_window_alignment_and_shares():
    """PRISM days Oct 22-23 use AWDB days Oct 21-22, with SWE before the window from Oct 20."""
    from validate_snow import prism_split, snotel_window, summarize
    series = {"PRCP": {"2026-10-21": 1.0, "2026-10-22": 1.0, "2026-10-23": 9.0},
              "WTEQ": {"2026-10-20": 0.5, "2026-10-21": 1.0, "2026-10-22": 0.8, "2026-10-23": 5.0},
              "TAVG": {"2026-10-21": 50.0, "2026-10-22": 30.2}}  # 10 °C (rain), -1 °C (snow)
    w = snotel_window(series, dt.date(2026, 10, 22), dt.date(2026, 10, 23))
    assert w["precip_mm"] == pytest.approx(50.8)                  # Oct 23's 9 in is outside the window
    assert w["swe_gain_mm"] == pytest.approx(0.5 * 25.4)          # peak 1.0 minus 0.5 before
    assert w["swe_net_mm"] == pytest.approx(0.3 * 25.4)
    assert w["station_split_snow_mm"] == pytest.approx(25.4)      # only the cold day's inch
    with pytest.raises(ValueError, match="precipitation missing"):
        snotel_window({"PRCP": {"2026-10-21": 1.0}}, dt.date(2026, 10, 22), dt.date(2026, 10, 23))
    assert prism_split([10, 20], [5.0, 1.5]) == (30.0, pytest.approx(10.0))
    rows = [{"status": "ok", "precip_mm": 50, "swe_gain_mm": 10, "prism_snow_mm": 12, "observed_snow_pct": 20,
             "station_split_snow_pct": 30, "prism_snow_pct": 24},
            {"status": "ok", "precip_mm": 5, "swe_gain_mm": 5, "prism_snow_mm": 0, "observed_snow_pct": 100,
             "station_split_snow_pct": 0, "prism_snow_pct": 0}]
    s = summarize(rows, min_precip_mm=10)
    assert s["stations"] == 1 and s["observed_snow_pct"] == 20 and s["prism_over_observed_snow"] == 1.2



def test_flow_cache_reuse_rules(tmp_path):
    """Results cut short by the time of the run are refetched once more flow exists; precip skips are redone
    when precipitation settings change, flow skips are not."""
    import json
    from types import SimpleNamespace
    from validate_runoff import flow_end_target, flow_stale, load_cached, window_key
    a = SimpleNamespace(after_days=7, max_gap_hours=12.0, minor_mm=1.0, regulated_mm=10.0, max_missing_pct=1.0,
                        res="800m")
    utc = dt.timezone.utc
    target = flow_end_target(dt.date(2026, 10, 27), 7)
    assert target == dt.datetime(2026, 11, 3, 12, tzinfo=utc)
    cut = dt.datetime(2026, 10, 31, 16, tzinfo=utc)
    assert not flow_stale(cut, target, dt.datetime(2026, 10, 31, 20, tzinfo=utc))   # nothing new yet
    assert flow_stale(cut, target, dt.datetime(2026, 11, 1, 13, tzinfo=utc))         # a day of new flow
    assert not flow_stale(target, target, dt.datetime(2026, 12, 1, tzinfo=utc))      # complete

    key = window_key([dt.date(2026, 10, 20), dt.date(2026, 10, 27)], a, [2] * 8)
    basin = gpd.GeoDataFrame(geometry=[box(-108.0, 37.8, -107.8, 38.0)], crs=4269)
    basin.to_file(tmp_path / "runoff_1_basin.geojson", driver="GeoJSON")
    (tmp_path / "runoff_1.json").write_text(json.dumps(
        {"run_key": key, "status": "ok", "flow_window_utc": {"end": cut.isoformat()}}))
    late = dt.datetime(2026, 11, 5, tzinfo=utc)
    assert load_cached(tmp_path, "1", key, target, now=dt.datetime(2026, 10, 31, 18, tzinfo=utc)) is not None
    assert load_cached(tmp_path, "1", key, target, now=late) is None

    other = window_key([dt.date(2026, 10, 20), dt.date(2026, 10, 27)],
                       SimpleNamespace(**{**vars(a), "max_missing_pct": 5.0}), [2] * 8)
    for stage, reused in (("precip", False), ("flow", True)):
        (tmp_path / "runoff_2.json").write_text(json.dumps(
            {"run_key": key, "status": "x", "stage": stage, "checked_utc": late.isoformat()}))
        got = load_cached(tmp_path, "2", other, target, now=late)
        assert (got is not None) == reused
    (tmp_path / "runoff_3.json").write_text(json.dumps({"run_key": key, "status": "old skip"}))  # undated
    assert load_cached(tmp_path, "3", key, target, now=late) is None
    assert window_key([D], SimpleNamespace(**{**vars(a), "res": "4km"}))["res"] == "4km"


def test_flow_series_hourly_gauge():
    from validate_runoff import flow_series
    t0 = pd.Timestamp("2026-10-21T12:00Z")
    hourly = [((t0 + pd.Timedelta(hours=h)).isoformat(), 10.0, [], "P") for h in range(25)]
    q, interp, info = flow_series(hourly, t0, t0 + pd.Timedelta(hours=24))
    assert info["reading_interval_min"] == 60 and info["longest_gap_h"] == 0 and info["gaps"] == []
    assert info["interpolated_h"] == pytest.approx(18.0)  # 3 of every 4 steps lie between readings
    off = [((t0 + pd.Timedelta(minutes=15 * k + 7)).isoformat(), 10.0, [], "P") for k in range(96)]
    assert flow_series(off, t0, t0 + pd.Timedelta(hours=24))[2]["interpolated_h"] <= 0.25  # readings at :07, :22


def test_ratios_with_all_snow_and_retry_after():
    from observations import retry_after_s
    from validate_runoff import precip_and_ratios, summary_row
    precip = [{"prism_day": D, "depth_mm": 10.0, "volume_m3": 1e6, "rain_mm": 0.0, "rain_m3": 0.0}]
    vols = {"runoff_constant_m3": 2e5, "runoff_line_m3": 1e5, "total_flow_m3": 3e5}
    out = precip_and_ratios(precip, 0.0, 1e8, vols)
    assert out["rain"]["snow_pct"] == 100 and out["runoff_ratio"]["rain_line_baseflow"] is None
    result = {"gauge": {"name": "x", "lon": 0, "lat": 0}, **out, "flow": {**vols, "peak_cfs": 1, "peak_time_utc": "",
              "interpolated_h": 0}, "regulation": {"class": "unregulated", "storage_mm": 0, "storage_af": 0,
              "n_dams": 0, "largest": []}, "basin_area_km2": {"prism_cells": 100, "usgs_drainage_area": None},
              "prism_window": {"start": "a", "end": "b"}, "flow_window_utc": {"end": "c"}}
    row = summary_row("1", result)
    assert row["snow_pct"] == 100 and row["rain_ratio_low"] is None
    assert retry_after_s("120", 5) == 120 and retry_after_s(None, 5) == 5
    assert retry_after_s("Wed, 21 Oct 2015 07:28:00 GMT", 5) == 0  # a date in the past
    assert retry_after_s("soon", 5) == 5


def test_snow_share_ignores_nan():
    from validate_snow import share, summarize
    assert share(10.0, float("nan")) is None and share(float("nan"), 10.0) is None and share(5.0, 0.0) is None
    rows = [{"status": "ok", "precip_mm": 50, "swe_gain_mm": 10, "prism_snow_mm": None, "observed_snow_pct": 20,
             "station_split_snow_pct": 30, "prism_snow_pct": None}]
    assert summarize(rows, 10)["prism_over_observed_snow"] is None


def test_gauge_runs_update_only_their_gauges(tmp_path, monkeypatch):
    """--gauge A then --gauge B keeps both in the layers (offline: network calls replaced by fakes)."""
    import subprocess
    import validate_runoff
    import observations
    from test_offline import OFFLINE, SCRIPT, make_zip
    out = tmp_path / "run"
    (out / "raw_zips").mkdir(parents=True)
    for ymd in ("20261021", "20261022"):
        make_zip(out / "raw_zips", ymd, 10.0)
    boundary = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs=4269).to_file(boundary)
    subprocess.run([sys.executable, str(SCRIPT), "--start", "2026-10-21", "--end", "2026-10-22", "--boundary",
                    str(boundary), "--out", str(out), "--pause", "0"], check=True, capture_output=True, env=OFFLINE)
    sites = {"A": (-108.3, 37.6), "B": (-108.1, 37.6)}

    def site(s):
        return {"site": s, "name": f"gauge {s}", "lon": sites[s][0], "lat": sites[s][1], "site_type": "Stream",
                "drainage_area_mi2": None}

    def basin(s):
        x, y = sites[s]
        return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                "geometry": box(x - 0.05, y - 0.05, x + 0.05, y + 0.05).__geo_interface__}]}

    def flow(s, t0, t1):
        times = pd.date_range(t0, t1, freq="15min")
        return [(t.isoformat(), 50.0 + 10 * np.sin(k / 20), [], "Provisional") for k, t in enumerate(times)]
    monkeypatch.setattr(observations, "nid_dams", lambda bbox: [])
    monkeypatch.setattr(observations, "usgs_sites", lambda bbox: {s: site(s) for s in sites})
    monkeypatch.setattr(observations, "usgs_site", site)
    monkeypatch.setattr(observations, "nldi_basin", basin)
    monkeypatch.setattr(observations, "usgs_continuous", flow)

    def run(*gauges):
        monkeypatch.setattr(sys, "argv", ["validate_runoff.py", "--out", str(out), "--boundary", str(boundary),
                                          "--gauge", *gauges])
        validate_runoff.main()
    run("A")
    run("B")
    run("B")
    gpkg = out / "validation" / "validation.gpkg"
    assert sorted(gpd.read_file(gpkg, layer="gauge_basins")["site"]) == ["A", "B"]
    gauges = gpd.read_file(gpkg, layer="gauges")
    assert sorted(gauges["site"]) == ["A", "B"] and gauges["ratio_low"].dtype.kind == "f"  # numbers, not text


def test_awdb_keeps_primary_sensor():
    from observations import merge_awdb_station

    def block(el, ordinal, value):
        return {"stationElement": {"elementCode": el, "ordinal": ordinal}, "values": [{"date": "2026-10-22", "value": value}]}
    st = {"data": [block("WTEQ", 2, 9.9), block("WTEQ", 1, 1.0), block("PRCP", 1, 0.5), block("PRCP", 2, 7.7)]}
    series = merge_awdb_station({}, st)
    assert series["WTEQ"]["2026-10-22"] == 1.0 and series["PRCP"]["2026-10-22"] == 0.5


def test_event_total_observation_time_change():
    """A station that moves from 7 AM to midnight readings maps two days onto one PRISM day."""
    tzo = -7.0
    recs = []
    for i, hour in enumerate((7, 7, 24, 24)):
        d = dt.date(2026, 10, 20) + dt.timedelta(days=i)
        pday, off = prism_day_for(d, hour, tzo)
        recs.append({"obs_date": d, "obs_hour": hour, "hour_known": True, "prism_day": pday, "offset_h": off,
                     "obs_mm": 1.0, "kind": "value"})
    days = sorted({r["prism_day"] for r in recs})
    days = [days[0] + dt.timedelta(days=k) for k in range((days[-1] - days[0]).days + 1)]
    assert event_total(recs, days) == (None, "observation time changed in the window")
