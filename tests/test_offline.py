"""Offline end-to-end test: synthetic PRISM-like zips + boundary, no network."""
import csv
import datetime as dt
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from pyproj import Geod
from shapely.geometry import box
import xarray as xr

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS / "prism_watershed.py"
sys.path.insert(0, str(SCRIPTS))
from find_huc import build_where  # noqa: E402
from storm_total import (boundary_volumes, cell_area_m2, cell_fractions, revision_change,  # noqa: E402
                         snow_fraction, window_total)


# Any request is sent to a closed local port, so it fails fast instead of reaching PRISM.
OFFLINE = {**os.environ, "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}
GRID = from_origin(-108.5, 37.8, 1 / 120, 1 / 120)


def make_zip(raw_dir, ymd, value, release=2, var="ppt"):
    tif = raw_dir / f"prism_{var}_us_30s_{ymd}.tif"
    with rasterio.open(tif, "w", driver="GTiff", height=100, width=100, count=1,
                       dtype="float32", crs="EPSG:4269",
                       transform=from_origin(-108.5, 37.8, 1 / 120, 1 / 120),
                       nodata=-9999) as dst:
        dst.write(np.broadcast_to(np.asarray(value, dtype="float32"), (100, 100)), 1)
    info = raw_dir / f"prism_{var}_us_30s_{ymd}.info.txt"
    info.write_text(f"PRISM_DATASET_VERSION: D2\nPRISM_DATASET_RELEASE_NUMBER: {release}\n")
    with zipfile.ZipFile(raw_dir / f"prism_{var}_us_30s_{ymd}.zip", "w") as z:
        z.write(tif, tif.name)
        z.write(info, info.name)
    tif.unlink()
    info.unlink()


def test_pipeline(tmp_path):
    out = tmp_path / "run"
    raw = out / "raw_zips"
    raw.mkdir(parents=True)
    make_zip(raw, "20261021", 10.0)
    make_zip(raw, "20261022", 30.0)
    boundary = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs="EPSG:4326").to_file(boundary)

    subprocess.run([sys.executable, str(SCRIPT), "--start", "2026-10-21", "--end", "2026-10-22",
                    "--boundary", str(boundary), "--out", str(out), "--pause", "0"], check=True)

    rows = list(csv.DictReader(open(out / "daily_basin_summary.csv")))
    assert [r["basin_mean_mm"] for r in rows] == ["10.0", "30.0"]
    assert all(r["release"] == "2" for r in rows)
    with rasterio.open(out / "ppt_800m_total_20261021_20261022.tif") as src:
        data = src.read(1, masked=True)
    assert np.isclose(data.mean(), 40.0)

    with xr.open_dataset(out / "ppt_800m_daily_clip.nc") as ds:
        assert ds.sizes["time"] == 2
        assert list(ds.release.values) == [2, 2]
        # CF: day-label time stamps are the midpoints of their 12:00-to-12:00 UTC bounds
        assert ds.attrs["Conventions"] == "CF-1.10" and ds.time.attrs["bounds"] == "time_bnds"
        b = ds.time_bnds.values[0]
        assert b[0] == np.datetime64("2026-10-20T12:00") and b[1] == np.datetime64("2026-10-21T12:00")
        assert ds.time.values[0] == np.datetime64("2026-10-21T00:00")
        assert ds.ppt.attrs["standard_name"] == "lwe_thickness_of_precipitation_amount"
        assert ds.ppt.attrs["cell_methods"] == "area: mean time: sum" and ds.cell_area.attrs["standard_name"] == "cell_area"
        r = window_total(ds, "2026-10-21", "2026-10-22")
        expected = 0.040 * float(ds.cell_area.where(ds.ppt.isel(time=0).notnull()).sum())
        assert np.isclose(r["volume_m3"], expected)
        assert np.isclose(r["mean_mm"], 40.0)
        assert np.isclose(float(window_total(ds, "2026-10-22", "2026-10-22")["mean_mm"]), 30.0)

    tif, vol_tif = tmp_path / "window.tif", tmp_path / "volume.tif"
    res = subprocess.run([sys.executable, str(SCRIPTS / "storm_total.py"), "--out", str(out),
                          "--start", "2026-10-22", "--end", "2026-10-22", "--tif", str(tif),
                          "--volume-tif", str(vol_tif), "--volume-nc", str(tmp_path / "volume.nc"),
                          "--boundary", str(boundary)],
                         check=True, capture_output=True, text=True)
    assert "30.0 mm" in res.stdout
    assert "r2 Oct 22" in res.stdout and "--refresh" in res.stdout
    assert "Boundary:    cell centers" in res.stdout  # national grids are in extracted/
    with rasterio.open(tif) as src:
        assert np.isclose(src.read(1, masked=True).mean(), 30.0)
    with rasterio.open(vol_tif) as src:
        assert src.units == ("m3",)
        cells = src.read(1, masked=True)
    window = window_total(xr.open_dataset(out / "ppt_800m_daily_clip.nc"), "2026-10-22", "2026-10-22")
    assert np.isclose(float(cells.sum()), window["volume_m3"], rtol=1e-5)
    with xr.open_dataset(tmp_path / "volume.nc") as vol:
        assert vol.volume.sizes["time"] == 1 and vol.volume.attrs["units"] == "m3"
        assert np.isclose(float(vol.volume.sum()), window["volume_m3"])
        assert np.isclose(float(vol.volume_total.sum()), window["volume_m3"])

    missing = subprocess.run([sys.executable, str(SCRIPTS / "storm_total.py"), "--out", str(out),
                              "--start", "2026-10-20", "--end", "2026-10-22"],
                             capture_output=True, text=True)
    assert missing.returncode != 0 and "2026-10-20" in missing.stderr


def test_new_release_replaces_day_and_keeps_history(tmp_path):
    """A newer zip for a day (as --refresh leaves it) is re-extracted, re-clipped and logged with the old one."""
    out = tmp_path / "run"
    raw = out / "raw_zips"
    raw.mkdir(parents=True)
    make_zip(raw, "20261021", 10.0, release=1)
    make_zip(raw, "20261022", 30.0, release=1)
    boundary = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs="EPSG:4326").to_file(boundary)
    cmd = [sys.executable, str(SCRIPT), "--start", "2026-10-21", "--end", "2026-10-22",
           "--boundary", str(boundary), "--out", str(out), "--pause", "0"]
    subprocess.run(cmd, check=True, capture_output=True)

    make_zip(raw, "20261022", 33.0, release=2)  # extracted/ still holds release 1 of this day
    res = subprocess.run(cmd, check=True, capture_output=True, text=True)
    assert "2026-10-22: release 1 -> 2: 30.0 -> 33.0 mm" in res.stdout

    rows = list(csv.DictReader(open(out / "daily_basin_summary.csv")))
    assert [(r["release"], r["basin_mean_mm"]) for r in rows] == [("1", "10.0"), ("2", "33.0")]

    one_day = cmd[:3] + ["2026-10-22", "--end", "2026-10-22"] + cmd[6:]  # earlier days stay in the summary
    subprocess.run(one_day, check=True, capture_output=True)
    rows = list(csv.DictReader(open(out / "daily_basin_summary.csv")))
    assert [r["date"] for r in rows] == ["2026-10-21", "2026-10-22"]
    history = list(csv.DictReader(open(out / "ppt_800m_release_history.csv")))
    assert [(r["date"], r["release"], r["basin_mean_mm"]) for r in history] == [
        ("2026-10-21", "1", "10.0"), ("2026-10-22", "1", "30.0"), ("2026-10-22", "2", "33.0")]
    v1, v2 = (float(r["basin_volume_m3"]) for r in history[1:])
    assert np.isclose(v2 / v1, 1.1, rtol=1e-4)
    with xr.open_dataset(out / "ppt_800m_daily_clip.nc") as ds:
        assert list(ds.release.values) == [1, 2]
        assert np.isclose(window_total(ds, "2026-10-22", "2026-10-22")["mean_mm"], 33.0)


def test_stale_totals(tmp_path):
    from prism_watershed import stale_totals
    for name in ("ppt_800m_total_20261020_20261027.tif", "ppt_800m_total_20261028_20261031.tif",
                 "ppt_800m_total_20261026_20261027.tif", "tmean_800m_total_20261020_20261027.tif"):
        (tmp_path / name).write_bytes(b"x")
    assert stale_totals(tmp_path, "ppt", "800m", [dt.date(2026, 10, 26)], "ppt_800m_total_20261026_20261027.tif") == [
        "ppt_800m_total_20261020_20261027.tif"]


class FakeResponse:
    def __init__(self, content):
        self.status_code, self.content, self.text = 200, content, ""
        self.headers = {"Content-Type": "application/zip"}


class FakeSession:
    def __init__(self, content):
        self.content, self.calls = content, 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return FakeResponse(self.content)


def test_refresh_day_archives_superseded_zip(tmp_path):
    from prism_watershed import due_for_refresh, refresh_day, zip_release
    raw, newer = tmp_path / "raw_zips", tmp_path / "newer"
    raw.mkdir()
    newer.mkdir()
    make_zip(raw, "20261022", 30.0, release=1)
    make_zip(newer, "20261022", 33.0, release=2)
    day = dt.date(2026, 10, 22)

    session = FakeSession((newer / "prism_ppt_us_30s_20261022.zip").read_bytes())
    assert refresh_day("ppt", "800m", "us", day, "", raw, session) == ("1", "2")
    assert zip_release(raw / "prism_ppt_us_30s_20261022.zip") == "2"
    assert zip_release(raw / "releases" / "prism_ppt_us_30s_20261022_r1.zip") == "1"

    # Same release again: nothing archived, the zip is just replaced.
    assert refresh_day("ppt", "800m", "us", day, "", raw, session) == ("2", "2")
    assert len(list((raw / "releases").iterdir())) == 1 and not list(raw.glob("*.part"))

    today = dt.date(2026, 11, 20)
    # With PRISM's release service: refresh exactly when it has a newer release, whatever the zip's age.
    assert due_for_refresh("2", dt.date(2026, 11, 19), today, 8, 7, available=3)
    assert not due_for_refresh("2", dt.date(2026, 10, 1), today, 8, 7, available=2)
    assert not due_for_refresh("2", dt.date(2026, 10, 1), today, 8, 7, available=0)  # day not listed
    # Without it: the age rule.
    assert due_for_refresh("2", dt.date(2026, 11, 1), today, 8, 7)
    assert not due_for_refresh("2", dt.date(2026, 11, 15), today, 8, 7)  # checked recently
    assert not due_for_refresh("8", dt.date(2026, 11, 1), today, 8, 7)   # final
    assert due_for_refresh("", dt.date(2026, 11, 1), today, 8, 7)        # unknown release


def test_refresh_skips_recent_zips(tmp_path):
    """--refresh makes no request for a zip downloaded within --refresh-age (the offline pipeline still runs)."""
    out = tmp_path / "run"
    raw = out / "raw_zips"
    raw.mkdir(parents=True)
    make_zip(raw, "20261022", 30.0, release=2)
    boundary = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs="EPSG:4326").to_file(boundary)
    res = subprocess.run([sys.executable, str(SCRIPT), "--start", "2026-10-22", "--end", "2026-10-22",
                          "--boundary", str(boundary), "--out", str(out), "--pause", "0", "--refresh"],
                         check=True, capture_output=True, text=True, env=OFFLINE)
    assert "release 2" in res.stdout and "Re-run later with --refresh" in res.stdout
    assert "request failed" not in res.stdout  # no download was attempted


def test_revision_change():
    history = [{"date": "2026-10-21", "release": "2", "basin_volume_m3": "100"},
               {"date": "2026-10-22", "release": "1", "basin_volume_m3": "200"},
               {"date": "2026-10-22", "release": "3", "basin_volume_m3": "230"},
               {"date": "2026-10-22", "release": "2", "basin_volume_m3": "250"}]
    days = [dt.date(2026, 10, 21), dt.date(2026, 10, 22)]
    rc = revision_change(history, days)
    assert (rc["first_m3"], rc["latest_m3"]) == (300, 330)  # releases ordered by number, not by row
    assert np.isclose(rc["change_pct"], 10.0) and rc["revised"] == [(days[1], "1", "3")]
    assert revision_change(history, [dt.date(2026, 10, 23)]) is None


def test_boundary_volumes_three_ways():
    transform = from_origin(-108.0, 38.0, 0.1, 0.1)
    # A 4x4 grid of 10 mm; the basin covers rows 1-2 of columns 0-2 and 40% of column 3 (center outside).
    geom = box(-108.0, 37.7, -107.66, 37.9)
    frac = cell_fractions(geom, transform, (4, 4))
    assert np.allclose(frac[1:3, :3], 1) and np.allclose(frac[1:3, 3], 0.4)
    assert frac[0].sum() == 0 and frac[3].sum() == 0
    total = np.full((4, 4), 10.0)
    total[2, 0] = np.nan  # no PRISM data in one cell: left out of every rule
    v = boundary_volumes(total, transform, geom)
    area = cell_area_m2(transform, 4)
    centers = 0.010 * (3 * area[1] + 2 * area[2])
    weighted = 0.010 * (3.4 * area[1] + 2.4 * area[2])
    touched = 0.010 * (4 * area[1] + 3 * area[2])
    assert np.isclose(v["weighted"], weighted) and np.isclose(v["touched"], touched)
    assert np.isclose(v["centers"], centers)


def test_snow_fraction():
    t = np.array([-2.0, 0.0, 1.5, 3.0, 8.0, np.nan])
    assert np.allclose(snow_fraction(t), [1, 1, 0.5, 0, 0, np.nan], equal_nan=True)
    assert np.allclose(snow_fraction(t, 1.0, 1.0), [1, 1, 0, 0, 0, np.nan], equal_nan=True)  # single threshold


def test_rain_snow_with_tmean_cube(tmp_path):
    """A tmean run in the same folder gets its own summary and cube; storm_total splits rain from snow."""
    out = tmp_path / "run"
    raw = out / "raw_zips"
    raw.mkdir(parents=True)
    for ymd, p, t in (("20261021", 10.0, -4.0), ("20261022", 30.0, 1.5)):  # all snow, then half snow
        make_zip(raw, ymd, p)
        make_zip(raw, ymd, t, var="tmean")
    boundary = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs="EPSG:4326").to_file(boundary)
    base = [sys.executable, str(SCRIPT), "--start", "2026-10-21", "--end", "2026-10-22",
            "--boundary", str(boundary), "--out", str(out), "--pause", "0"]
    subprocess.run(base, check=True, capture_output=True)
    subprocess.run(base + ["--var", "tmean"], check=True, capture_output=True)
    rows = list(csv.DictReader(open(out / "daily_basin_summary.csv")))
    assert [r["basin_mean_mm"] for r in rows] == ["10.0", "30.0"]  # untouched by the tmean run
    trows = list(csv.DictReader(open(out / "tmean_800m_daily_basin_summary.csv")))
    assert [(r["basin_mean"], r["units"]) for r in trows] == [("-4.0", "degC"), ("1.5", "degC")]
    assert not list(out.glob("tmean_*_total_*.tif"))

    nc = tmp_path / "rs.nc"
    res = subprocess.run([sys.executable, str(SCRIPTS / "storm_total.py"), "--out", str(out), "--boundary",
                          str(boundary), "--rain-snow-nc", str(nc), "--snow-tif", str(tmp_path / "snow.tif")],
                         check=True, capture_output=True, text=True)
    assert "Rain/snow:   rain 15.0 mm" in res.stdout and "snow 25.0 mm" in res.stdout  # 0+15 rain, 10+15 snow
    assert "rain + snow = precipitation volume" in res.stdout
    with xr.open_dataset(nc) as ds:
        assert np.isclose(float(ds.rain_total.mean()), 15.0) and np.isclose(float(ds.snow_total.mean()), 25.0)
    with rasterio.open(tmp_path / "snow.tif") as src:
        assert np.isclose(src.read(1, masked=True).mean(), 25.0)


def basin_file(tmp_path, geom):
    path = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4269").to_file(path)
    return path


def run_watershed(out, boundary, start, end, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), "--start", start, "--end", end, "--boundary", str(boundary),
                           "--out", str(out), "--pause", "0", *extra], capture_output=True, text=True, env=OFFLINE)


def test_clip_keeps_the_right_cells(tmp_path):
    """A gradient raster and a cell-aligned basin: the clip must keep exactly the cells inside."""
    out = tmp_path / "run"
    (out / "raw_zips").mkdir(parents=True)
    grad = np.arange(100 * 100, dtype="float32").reshape(100, 100) / 100  # unique value per cell
    make_zip(out / "raw_zips", "20261021", grad)
    rows, cols = slice(10, 30), slice(20, 50)  # 20 x 30 cells
    geom = box(-108.5 + 20 / 120, 37.8 - 30 / 120, -108.5 + 50 / 120, 37.8 - 10 / 120)
    res = run_watershed(out, basin_file(tmp_path, geom), "2026-10-21", "2026-10-21")
    assert res.returncode == 0, res.stdout + res.stderr
    area = np.repeat(cell_area_m2(GRID, 100)[:, None], 100, axis=1)
    expected_m3 = float((grad[rows, cols] / 1000 * area[rows, cols]).sum())
    with xr.open_dataset(out / "ppt_800m_daily_clip.nc") as ds:
        assert int(ds.ppt.notnull().sum()) == 600
        assert np.isclose(window_total(ds, "2026-10-21", "2026-10-21")["volume_m3"], expected_m3, rtol=1e-6)
    row = next(csv.DictReader(open(out / "daily_basin_summary.csv")))
    weighted = float((grad[rows, cols] * area[rows, cols]).sum() / area[rows, cols].sum())
    assert float(row["basin_mean_mm"]) == pytest.approx(weighted, abs=0.006)  # area-weighted, rounded to 0.01
    assert (out / "boundary.geojson").exists()  # --boundary is copied into the folder


def test_cube_coverage_uses_every_day(tmp_path):
    """A cell missing on one day is not 'outside PRISM coverage'; a cell missing on every day is."""
    out = tmp_path / "run"
    (out / "raw_zips").mkdir(parents=True)
    day1 = np.full((100, 100), 5.0, dtype="float32")
    day1[20:30, 30:40] = -9999          # no data on every day: 100 cells
    day2 = day1.copy()
    day2[40:45, 30:40] = -9999          # one-day gap: 50 more cells
    make_zip(out / "raw_zips", "20261021", day1)
    make_zip(out / "raw_zips", "20261022", day2)
    geom = box(-108.5 + 20 / 120, 37.8 - 50 / 120, -108.5 + 60 / 120, 37.8 - 10 / 120)  # 40 x 40 cells
    res = run_watershed(out, basin_file(tmp_path, geom), "2026-10-21", "2026-10-22")
    assert "WARN  nodata_inside" in res.stdout or "FAIL  nodata_inside" in res.stdout
    area = np.repeat(cell_area_m2(GRID, 100)[:, None], 100, axis=1)
    expected = float(area[20:30, 30:40].sum() / area[10:50, 20:60].sum() * 100)
    with xr.open_dataset(out / "ppt_800m_daily_clip.nc") as ds:
        assert ds.attrs["basin_without_prism_pct"] == pytest.approx(expected, abs=0.01)


def test_coverage_counts_basin_beyond_grid_edge():
    from prism_watershed import prism_coverage
    from storm_total import geodesic_km2
    arr = np.ma.masked_array(np.ones((12, 12)), mask=False)
    transform = from_origin(-108.0, 38.0, 0.1, 0.1)      # grid covers 36.8-38.0 N
    basin = gpd.GeoDataFrame(geometry=[box(-107.5, 37.5, -107.0, 38.5)], crs=4269)  # half beyond the top edge
    pct, missing_km2, basin_km2 = prism_coverage(arr, transform, basin, grid_bounds=(-108.0, 36.8, -106.8, 38.0))
    beyond = geodesic_km2(box(-107.5, 38.0, -107.0, 38.5))
    assert missing_km2 == pytest.approx(beyond, rel=1e-6) and pct == pytest.approx(beyond / basin_km2 * 100)
    assert prism_coverage(arr, transform, basin)[0] == 0.0  # without the grid bounds the gap is invisible


def test_dry_window_and_missing_day(tmp_path):
    """Zero precipitation must not crash storm_total; a missing day fails QC and skips the event total."""
    out = tmp_path / "run"
    (out / "raw_zips").mkdir(parents=True)
    make_zip(out / "raw_zips", "20261021", 0.0)
    make_zip(out / "raw_zips", "20261022", 0.0)
    boundary = basin_file(tmp_path, box(-108.4, 37.5, -108.0, 37.7))
    res = run_watershed(out, boundary, "2026-10-21", "2026-10-23")  # Oct 23 can't be downloaded
    assert res.returncode == 1 and "Traceback" not in res.stderr
    assert "2026-10-23" in res.stdout and "request failed" in res.stdout
    assert "Event total not written: 1 of 3 days missing" in res.stdout
    assert "FAIL  completeness" in res.stdout and not list(out.glob("ppt_800m_total_*.tif"))
    dry = subprocess.run([sys.executable, str(SCRIPTS / "storm_total.py"), "--out", str(out), "--boundary",
                          str(boundary)], capture_output=True, text=True)
    assert dry.returncode == 0, dry.stderr
    assert "Volume:      0 m³" in dry.stdout and "range n/a" in dry.stdout


def test_refresh_with_corrupt_files(tmp_path):
    """A corrupt cached zip is set aside instead of crashing --refresh; a corrupt refreshed download is discarded."""
    from prism_watershed import refresh_day, zip_release
    out = tmp_path / "run"
    raw = out / "raw_zips"
    raw.mkdir(parents=True)
    make_zip(raw, "20261021", 10.0)
    (raw / "prism_ppt_us_30s_20261022.zip").write_bytes(b"truncated")
    old = dt.datetime(2026, 10, 1).timestamp()
    for z in raw.glob("*.zip"):
        os.utime(z, (old, old))
    res = run_watershed(out, basin_file(tmp_path, box(-108.4, 37.5, -108.0, 37.7)), "2026-10-21", "2026-10-22",
                       "--refresh")
    assert "Traceback" not in res.stderr and (raw / "prism_ppt_us_30s_20261022.zip.bad").exists()
    assert res.returncode == 1  # Oct 21 refresh fails (offline), Oct 22 is corrupt

    session = FakeSession(b"not a zip")
    assert refresh_day("ppt", "800m", "us", dt.date(2026, 10, 21), "", raw, session) is None
    assert zip_release(raw / "prism_ppt_us_30s_20261021.zip") == "2" and not list(raw.glob("*.part"))


def test_tiny_basin_exits_cleanly(tmp_path):
    out = tmp_path / "run"
    (out / "raw_zips").mkdir(parents=True)
    make_zip(out / "raw_zips", "20261021", 5.0)
    tiny = box(-108.5 + 10.1 / 120, 37.8 - 10.4 / 120, -108.5 + 10.3 / 120, 37.8 - 10.1 / 120)  # misses every center
    res = run_watershed(out, basin_file(tmp_path, tiny), "2026-10-21", "2026-10-21")
    assert res.returncode == 2 and "covers no PRISM cell center" in res.stderr and "Traceback" not in res.stderr


def test_summary_names():
    from prism_watershed import summary_name
    assert summary_name("ppt", "800m") == "daily_basin_summary.csv"
    assert summary_name("ppt", "4km") == "ppt_4km_daily_basin_summary.csv"
    assert summary_name("tmean", "800m") == "tmean_800m_daily_basin_summary.csv"


def test_cell_area_matches_pyproj():
    transform = from_origin(-108.5, 37.8, 1 / 120, 1 / 120)
    areas = cell_area_m2(transform, 3)
    g = Geod(ellps="GRS80")
    for row, area in enumerate(areas):
        top = 37.8 - row / 120
        ref, _ = g.polygon_area_perimeter([0, 1 / 120, 1 / 120, 0], [top - 1 / 120] * 2 + [top] * 2)
        assert np.isclose(area, abs(ref), rtol=1e-6)


def test_find_huc_where_clause():
    assert build_where("san juan") == "UPPER(name) LIKE '%SAN JUAN%'"
    assert build_where("o'neill", "co") == "UPPER(name) LIKE '%O''NEILL%' AND states LIKE '%CO%'"


def test_requires_basin_and_valid_huc(tmp_path):
    def run(*args):
        return subprocess.run([sys.executable, str(SCRIPT), "--start", "2026-10-21", "--end", "2026-10-21",
                               "--out", str(tmp_path), *args], capture_output=True, text=True)
    missing = run()
    assert missing.returncode == 2 and "one of the arguments --huc --boundary is required" in missing.stderr
    bad = run("--huc", "140")
    assert bad.returncode != 0 and "--huc must be" in bad.stderr


def test_parse_release_rows():
    from prism_watershed import parse_release_rows
    rows = [["2026-10-26", "2026-10-31", "ppt", "2", "https://services.nacse.org/prism/data/get/us/800m/ppt/20261026"],
            ["2026-10-28", "2026-10-29", "ppt", "1", "url"], ["bad"], None]
    assert parse_release_rows(rows) == {dt.date(2026, 10, 26): 2, dt.date(2026, 10, 28): 1}


def test_utm_crs_keeps_the_datum():
    # NAD83 data gets a NAD83 zone, so a buffer there and back needs no datum transformation (none PROJ can't make)
    from storm_total import utm_crs
    basin = gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs=4269)
    assert utm_crs(basin).to_epsg() == 26912
    assert utm_crs(basin.to_crs(4326)).to_epsg() == 32612
    zone = basin.to_crs(utm_crs(basin)).buffer(10_000).to_crs(4269)
    assert np.isfinite(zone.total_bounds).all() and zone.total_bounds[0] < -108.4
