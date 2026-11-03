"""Tests for build_qgis_project.py helpers that don't need QGIS."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_qgis_project import (diff_breaks, discover, is_number, nice_breaks, nice_round, primary_total, tif_kind, utm_epsg,
                                volume_range_km3, window_label,  # noqa: E402
                                version_key, window_label)


def test_nice_breaks():
    assert nice_breaks(254.3)[-1] == 300 and len(nice_breaks(254.3)) <= 8
    assert nice_breaks(48.8)[-1] == 50
    assert nice_breaks(0) == [1]
    b = nice_breaks(1000)
    assert b == sorted(b) and len(b) <= 8
    wet = nice_breaks(254.3, 28.8)  # every cell got rain: classes start at the storm's minimum
    assert wet[0] == 25 and wet[-1] == 300 and len(wet) <= 8


def test_diff_breaks_follow_the_data():
    small = diff_breaks([0.2, 0.4, 0.9, 1.1, 2.5, 3.0, -0.5, -1.4])
    big = diff_breaks([5, 12, 30, -8, -22, 40, 60])
    assert small[3] < big[3] and small == [-x for x in reversed(small)]
    assert diff_breaks([]) == [-25, -10, -3, 3, 10, 25]
    assert nice_round(3.2) == 5 and nice_round(0.1) == 0.5 and nice_round(12) == 20


def test_utm_and_labels():
    assert utm_epsg(-121.2) == 26910 and utm_epsg(-108.7) == 26912 and utm_epsg(-81.0) == 26917
    assert window_label(dt.date(2026, 10, 20), dt.date(2026, 10, 27)) == "Oct 20–27, 2026"
    assert window_label(dt.date(2026, 10, 30), dt.date(2026, 11, 2)) == "Oct 30 – Nov 2, 2026"


def test_version_key():
    assert sorted(["3.44.10", "4.2.3", "4.2.10"], key=version_key) == ["3.44.10", "4.2.3", "4.2.10"]


def test_discover(tmp_path):
    (tmp_path / "clipped").mkdir()
    for d in ("20261021", "20261020"):
        (tmp_path / "clipped" / f"ppt_800m_{d}_clip.tif").write_bytes(b"x")
        (tmp_path / "clipped" / f"tmean_800m_{d}_clip.tif").write_bytes(b"x")  # not mapped
    (tmp_path / "ppt_800m_total_20261020_20261021.tif").write_bytes(b"x")
    (tmp_path / "volume_m3.tif").write_bytes(b"x")
    (tmp_path / "wbd_huc1408.geojson").write_text("{}")
    f = discover(tmp_path)
    assert sorted(f["days"]) == [dt.date(2026, 10, 20), dt.date(2026, 10, 21)]
    assert [p.name for p in f["totals"]] == ["ppt_800m_total_20261020_20261021.tif"]
    assert [p.name for p in f["other_tifs"]] == ["volume_m3.tif"]
    assert f["wbd"][0].name == "wbd_huc1408.geojson"


def test_pick_qgis_prefers_v3():
    from build_qgis_project import pick_qgis
    apps = [("4.2.3", "/a/4.2.3"), ("4.2.2", "/a/4.2.2"), ("3.44.10", "/a/3.44"), ("3.40.1", "/a/3.40")]
    assert pick_qgis(apps)[0] == ("3.44.10", "/a/3.44")
    assert pick_qgis([("4.2.3", "/a/4.2.3")])[0] == ("4.2.3", "/a/4.2.3")
    assert pick_qgis([])[0] is None


def test_is_number():
    class Null:  # stands in for PyQGIS's NULL
        pass
    assert is_number(3) and is_number(2.5) and not is_number(None) and not is_number(Null())
    assert not is_number(float("nan")) and not is_number(True)


def test_tif_kind():
    assert tif_kind("volume_m3") == "volume" and tif_kind("ppt_800m_volume_m3_20261020_20261027") == "volume"
    assert tif_kind("rain_mm_20261020_20261027") == "rain" and tif_kind("snow_mm_20261020_20261027") == "snow"
    assert tif_kind("depth_mm") == "depth"
    assert tif_kind("pct_of_normal_20261020_20261027") == "percent"


def test_volume_range_km3(tmp_path):
    import json
    (tmp_path / "ppt_800m_uncertainty_20261020_20261027.json").write_text(json.dumps({"range_m3": [4.6e9, 4.9e9]}))
    assert volume_range_km3(tmp_path, "2026-10-20", "2026-10-27", "800m") == (4.6, 4.9)
    assert volume_range_km3(tmp_path, "2026-10-20", "2026-10-31") is None



def test_nice_breaks_edges():
    for lo, hi in ((0, 3000), (1500, 3000), (1000, 1000), (40, 40), (0, 0.3)):
        b = nice_breaks(hi, lo)
        assert b == sorted(b) and b[-1] >= hi and len(b) >= 2, (lo, hi, b)  # at least one colored class
    assert nice_breaks(3000)[-1] == 3000


def test_tif_kind_uses_whole_words():
    assert tif_kind("terrain") is None and tif_kind("drainage_area") is None and tif_kind("hillshade") is None
    assert tif_kind("storm3_depth_mm") == "depth" and tif_kind("rain_mm_20261020_20261027") == "rain"
    assert tif_kind("snow_mm") == "snow" and tif_kind("ppt_800m_volume_m3_20261020_20261027") == "volume"


def test_window_label_one_day():
    assert window_label(dt.date(2026, 10, 20), dt.date(2026, 10, 20)) == "Oct 20, 2026"


def test_discover_prefers_800m_and_picks_total(tmp_path):
    import json
    (tmp_path / "clipped").mkdir()
    for res in ("800m", "4km"):
        (tmp_path / "clipped" / f"ppt_{res}_20261020_clip.tif").write_bytes(b"x")
        (tmp_path / f"ppt_{res}_uncertainty_20261020_20261027.json").write_text(json.dumps(
            {"volume_m3": 4.7e9 if res == "800m" else 4.5e9, "range_m3": [1e9, 2e9] if res == "4km" else [4.6e9, 4.9e9]}))
    totals = [tmp_path / n for n in ("ppt_800m_total_20261020_20261027.tif", "ppt_800m_total_20261026_20261027.tif",
                                     "ppt_800m_total_20261020_20261031.tif")]
    for t in totals:
        t.write_bytes(b"x")
    (tmp_path / "boundary.geojson").write_text("{}")
    f = discover(tmp_path)
    assert f["res"] == "800m" and f["other_res"] == ["4km"] and f["wbd"][0].name == "boundary.geojson"
    assert all("800m" in p.name for p in f["daily"] + f["totals"])
    assert primary_total(f["totals"]).name == "ppt_800m_total_20261020_20261031.tif"  # longest
    assert primary_total(f["totals"], dt.date(2026, 10, 20), dt.date(2026, 10, 27)) == totals[0]
    assert volume_range_km3(tmp_path, "2026-10-20", "2026-10-27", "800m") == (4.6, 4.9)
    assert volume_range_km3(tmp_path, "2026-10-20", "2026-10-27", volume_m3=4.7e9) == (4.6, 4.9)  # not 4km's
    assert volume_range_km3(tmp_path, "2026-10-20", "2026-10-27", volume_m3=3.0e9) is None


def test_build_real_project(tmp_path):
    """Build a project under QGIS (skipped without QGIS) from a folder with the awkward cases the review found."""
    import subprocess
    import zipfile
    import geopandas as gpd
    from shapely.geometry import Point, box
    from build_qgis_project import find_qgis_python
    from test_offline import OFFLINE, SCRIPT, make_zip
    if not find_qgis_python()[0]:
        pytest.skip("QGIS not installed")
    out = tmp_path / "Test_Basin"
    (out / "raw_zips").mkdir(parents=True)
    for ymd in ("20261021", "20261022"):
        make_zip(out / "raw_zips", ymd, 10.0)
    boundary = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs=4269).to_file(boundary)
    for s, e in (("2026-10-21", "2026-10-22"), ("2026-10-22", "2026-10-22")):
        subprocess.run([sys.executable, str(SCRIPT), "--start", s, "--end", e, "--boundary", str(boundary),
                        "--out", str(out), "--pause", "0"], check=True, capture_output=True, env=OFFLINE)
    (out / "terrain.tif").write_bytes((out / "ppt_800m_total_20261021_20261022.tif").read_bytes())
    (out / "validation").mkdir()
    gpd.GeoDataFrame([{"triplet": "1:CO:SNTL", "name": "A", "status": "precipitation missing"}],
                     geometry=[Point(-108.2, 37.6)], crs=4269).to_file(out / "validation" / "validation.gpkg",
                                                                        layer="snotel")  # every station skipped
    res = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "build_qgis_project.py"),
                          "--out", str(out)], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Not mapped (name doesn't say what it holds): terrain.tif" in res.stdout
    with zipfile.ZipFile(out / "QGIS" / "Test_Basin.qgz") as z:
        qgs = z.read(next(n for n in z.namelist() if n.endswith(".qgs"))).decode()
    assert "SNOTEL: PRISM" in qgs and "Study basin" in qgs and "terrain.tif" not in qgs
    assert "Precipitation depth (mm), Oct 21–22, 2026" in qgs and "Precipitation depth (mm), Oct 22, 2026" in qgs
