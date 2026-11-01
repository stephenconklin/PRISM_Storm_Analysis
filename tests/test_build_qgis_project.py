"""Tests for build_qgis_project.py helpers that don't need QGIS."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_qgis_project import (diff_breaks, discover, is_number, nice_breaks, nice_round, tif_kind, utm_epsg,  # noqa: E402
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
