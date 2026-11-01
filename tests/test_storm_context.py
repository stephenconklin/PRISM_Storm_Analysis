"""Storm context (percent of normal), offline: normals zips pre-seeded so nothing is downloaded."""
import datetime as dt
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from storm_context import normal_name, window_weights  # noqa: E402
from test_offline import GRID, OFFLINE, SCRIPT, make_zip  # noqa: E402


def test_normal_names_and_weights():
    assert normal_name("ppt", "800m", "us", 10) == "prism_ppt_us_30s_202010_avg_30y.zip"
    assert normal_name("ppt", "4km", "us", 2, 29) == "prism_ppt_us_25m_20200229_avg_30y.zip"
    w = window_weights(dt.date(2026, 10, 30), dt.date(2026, 11, 2))
    assert w == [(10, None, pytest.approx(2 / 31)), (11, None, pytest.approx(2 / 30))]
    assert window_weights(dt.date(2026, 10, 30), dt.date(2026, 10, 31), daily=True) == [(10, 30, 1.0), (10, 31, 1.0)]


def seed_normal(zdir, name, value):
    tif = zdir / name.replace(".zip", ".tif")
    with rasterio.open(tif, "w", driver="GTiff", height=100, width=100, count=1, dtype="float32",
                       crs="EPSG:4269", transform=GRID, nodata=-9999) as dst:
        dst.write(np.full((100, 100), value, dtype="float32"), 1)
    with zipfile.ZipFile(zdir / name, "w") as z:
        z.write(tif, tif.name)
    tif.unlink()


def test_percent_of_normal_end_to_end(tmp_path):
    out = tmp_path / "run"
    (out / "raw_zips").mkdir(parents=True)
    make_zip(out / "raw_zips", "20261021", 10.0)
    make_zip(out / "raw_zips", "20261022", 20.0)
    boundary = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs=4269).to_file(boundary)
    subprocess.run([sys.executable, str(SCRIPT), "--start", "2026-10-21", "--end", "2026-10-22", "--boundary",
                    str(boundary), "--out", str(out), "--pause", "0"], check=True, capture_output=True, env=OFFLINE)
    (out / "normals_zips").mkdir()
    seed_normal(out / "normals_zips", "prism_ppt_us_30s_202010_avg_30y.zip", 31.0)  # 1 mm/day in October
    res = subprocess.run([sys.executable, str(SCRIPTS / "storm_context.py"), "--out", str(out),
                          "--pct-tif", str(out / "pct_of_normal.tif")], capture_output=True, text=True, env=OFFLINE)
    assert res.returncode == 0, res.stdout + res.stderr
    j = json.loads((out / "ppt_800m_normals_20261021_20261022.json").read_text())
    assert j["window_normal_mm"] == pytest.approx(2.0) and j["percent_of_normal"] == pytest.approx(1500.0)
    assert j["months"][0]["storm_pct_of_month"] == pytest.approx(30 / 31 * 100, abs=0.1)
    with rasterio.open(out / "pct_of_normal.tif") as src:
        assert src.read(1, masked=True).mean() == pytest.approx(1500.0)
