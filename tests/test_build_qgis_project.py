"""Tests for build_qgis_project.py helpers that don't need QGIS."""
import datetime as dt
import re
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
    clipped, totals = tmp_path / "daily" / "clipped", tmp_path / "events" / "storm" / "totals"
    for d in (clipped, totals, tmp_path / "basin"):
        d.mkdir(parents=True)
    for d in ("20261021", "20261020"):
        (clipped / f"ppt_800m_{d}_clip.tif").write_bytes(b"x")
        (clipped / f"tmean_800m_{d}_clip.tif").write_bytes(b"x")  # not mapped
    (totals / "ppt_800m_total_20261020_20261021.tif").write_bytes(b"x")
    (totals / "ppt_800m_volume_20261020_20261021.tif").write_bytes(b"x")
    (tmp_path / "volume_m3.tif").write_bytes(b"x")  # put at the top by hand: mapped if its name says what it is
    (tmp_path / "basin" / "wbd_huc1408.geojson").write_text("{}")
    f = discover(tmp_path)
    assert sorted(f["days"]) == [dt.date(2026, 10, 20), dt.date(2026, 10, 21)]
    assert [p.name for p in f["totals"]] == ["ppt_800m_total_20261020_20261021.tif"]
    assert [p.name for p in f["other_tifs"]] == ["ppt_800m_volume_20261020_20261021.tif", "volume_m3.tif"]
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
    totals = tmp_path / "events" / "20261020_20261027" / "totals"
    totals.mkdir(parents=True)
    (totals / "ppt_800m_uncertainty_20261020_20261027.json").write_text(json.dumps({"range_m3": [4.6e9, 4.9e9]}))
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
    from layout import Layout
    lay = Layout(tmp_path, settings={})
    lay.clipped_dir.mkdir(parents=True)
    lay.basin_dir.mkdir()
    for res in ("800m", "4km"):
        (lay.clipped_dir / f"ppt_{res}_20261020_clip.tif").write_bytes(b"x")
        u = lay.uncertainty_json(res, "2026-10-20", "2026-10-27")
        u.parent.mkdir(parents=True, exist_ok=True)
        u.write_text(json.dumps({"volume_m3": 4.7e9 if res == "800m" else 4.5e9,
                                 "range_m3": [1e9, 2e9] if res == "4km" else [4.6e9, 4.9e9]}))
    totals = [lay.window_tif("total", "800m", s, e) for s, e in (("2026-10-20", "2026-10-27"),
                                                                ("2026-10-26", "2026-10-27"), ("2026-10-20", "2026-10-31"))]
    for t in totals:
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(b"x")
    (lay.basin_dir / "boundary.geojson").write_text("{}")
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
    from test_offline import OFFLINE, SCRIPT, make_zip, zips
    if not find_qgis_python()[0]:
        pytest.skip("QGIS not installed")
    out = tmp_path / "Test_Basin"
    for ymd in ("20261021", "20261022"):
        make_zip(zips(out), ymd, 10.0)
    boundary = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs=4269).to_file(boundary)
    for s, e in (("2026-10-21", "2026-10-22"), ("2026-10-22", "2026-10-22")):
        subprocess.run([sys.executable, str(SCRIPT), "--start", s, "--end", e, "--boundary", str(boundary),
                        "--out", str(out), "--pause", "0"], check=True, capture_output=True, env=OFFLINE)
    (out / "terrain.tif").write_bytes((out / "daily" / "ppt_800m_total_20261021_20261022.tif").read_bytes())
    vdir = out / "events" / "20261021_20261022" / "validation"
    vdir.mkdir(parents=True)
    gpd.GeoDataFrame([{"triplet": "1:CO:SNTL", "name": "A", "status": "precipitation missing"}],
                     geometry=[Point(-108.2, 37.6)], crs=4269).to_file(vdir / "validation.gpkg",
                                                                        layer="snotel_20261021_20261022")  # all skipped
    res = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "build_qgis_project.py"),
                          "--out", str(out)], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Not mapped (name doesn't say what it holds): terrain.tif" in res.stdout
    with zipfile.ZipFile(out / "qgis" / "Test_Basin.qgz") as z:
        qgs = z.read(next(n for n in z.namelist() if n.endswith(".qgs"))).decode()
    assert "SNOTEL: PRISM" in qgs and "Study basin" in qgs and "terrain.tif" not in qgs
    assert "Precipitation depth (mm), Oct 21–22, 2026" in qgs and "Precipitation depth (mm), Oct 22, 2026" in qgs


# ---------- several storms in one folder ----------

def run_script(args):
    """Run a script offline; on failure, show its output (CI keeps nothing else)."""
    import subprocess
    from test_offline import OFFLINE
    res = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, env=OFFLINE)
    assert res.returncode == 0, f"{Path(str(args[0])).name} exited {res.returncode}\n{res.stdout[-4000:]}\n{res.stderr[-4000:]}"


def two_storm_folder(tmp_path):
    """A folder with two events in storm.toml, their totals, window-named validation and totals layers in each
    event's folder, and the download window's total (no event)."""
    import json
    import geopandas as gpd
    from shapely.geometry import Point, box
    from test_offline import SCRIPT, make_zip, zips
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import config
    from layout import Layout
    from validate_stations import write_layer
    out = tmp_path / "Two_Storms"
    for ymd, v in (("20261021", 10.0), ("20261022", 30.0), ("20261023", 0.0), ("20261024", 20.0)):
        make_zip(zips(out), ymd, v)
    boundary = tmp_path / "basin.geojson"
    poly = box(-108.4, 37.5, -108.0, 37.7)
    gpd.GeoDataFrame(geometry=[poly], crs=4269).to_file(boundary)
    run_script([SCRIPT, "--start", "2026-10-21", "--end", "2026-10-24", "--boundary", boundary, "--out", out, "--pause", "0"])
    s = {"name": "Two storms", "boundary": str(boundary), "res": "800m",
         "download": {"start": dt.date(2026, 10, 21), "end": dt.date(2026, 10, 24)},
         "events": [{"name": "first", "start": dt.date(2026, 10, 21), "end": dt.date(2026, 10, 22)},
                    {"name": "second", "start": dt.date(2026, 10, 24), "end": dt.date(2026, 10, 24)},
                    {"name": "planned", "start": dt.date(2026, 10, 23), "end": dt.date(2026, 10, 23)}]}  # not run
    (out / "storm.toml").write_text(config.render(s))
    lay = Layout(out)
    for start, end in (("2026-10-21", "2026-10-22"), ("2026-10-24", "2026-10-24")):
        sp = f"{start.replace('-', '')}_{end.replace('-', '')}"
        run_script([SCRIPT.parent / "storm_total.py", "--out", out, "--start", start, "--end", end,
                    "--tif", lay.window_tif("total", "800m", start, end),
                    "--volume-tif", lay.window_tif("volume", "800m", start, end)])
        mean = 40.0 if sp.startswith("20261021") else 20.0
        bt = {"name": "Test basin", "huc": None, "prism_start": start, "prism_end": end, "area_km2": 784.1,
              "coverage_pct": 100.0, "mean_mm": mean, "max_mm": mean, "volume_m3": mean * 784.1e3,
              "volume_km3": mean * 784.1e-6, "volume_acre_ft": round(mean * 784.1e3 / 1233.48), "res": "800m"}
        write_layer(gpd.GeoDataFrame([bt], geometry=[poly], crs=4269), lay.totals_gpkg(start, end), "basin_total",
                    window=(start, end))
        st = {"station": "A", "in_stats": True, "in_sample": False, "diff_mm": -2.0 if mean > 30 else 3.0,
              "obs_mm": mean, "prism_mm": mean - 2}
        write_layer(gpd.GeoDataFrame([st], geometry=[Point(-108.2, 37.6)], crs=4269), lay.validation_gpkg(start, end),
                    "stations", window=(start, end))
    # SNOTEL only for the first window
    write_layer(gpd.GeoDataFrame([{"triplet": "1:CO:SNTL", "name": "S", "status": "ok"}], geometry=[Point(-108.1, 37.6)],
                                 crs=4269), lay.validation_gpkg("2026-10-21", "2026-10-22"), "snotel",
                window=("2026-10-21", "2026-10-22"))
    return out


def test_plan_events_groups_each_storm(tmp_path):
    from build_qgis_project import gpkg_layers, plan_events
    out = two_storm_folder(tmp_path)
    assert gpkg_layers(out / "events" / "first" / "validation" / "validation.gpkg") == {
        "stations_20261021_20261022", "snotel_20261021_20261022"}
    assert gpkg_layers(out / "events" / "second" / "validation" / "validation.gpkg") == {"stations_20261024_20261024"}
    events, other = plan_events(discover(out))
    assert [e["name"] for e in events] == ["first", "second"]  # 'planned' has no outputs yet
    first, second = events
    assert first["label"] == "first — Oct 21–22, 2026" and first["span"] == "20261021_20261022"
    assert {k: p.name for k, p in first["rasters"].items()} == {"depth": "ppt_800m_total_20261021_20261022.tif",
                                                                 "volume": "ppt_800m_volume_20261021_20261022.tif"}
    assert first["layers"]["stations"][1] == "stations_20261021_20261022"
    assert first["layers"]["basin_total"][1] == "basin_total_20261021_20261022"
    assert first["layers"]["snotel"][1] == "snotel_20261021_20261022"
    assert "snotel" not in second["layers"]
    assert second["layers"]["stations"][1] == "stations_20261024_20261024"
    assert [p.name for k, p in other] == ["ppt_800m_total_20261021_20261024.tif"]  # the download window's total


def test_plan_events_without_settings_uses_uncertainty_files(tmp_path):
    import json
    from build_qgis_project import plan_events
    out = tmp_path / "f"
    (out / "daily" / "clipped").mkdir(parents=True)
    (out / "daily" / "clipped" / "ppt_800m_20261021_clip.tif").write_bytes(b"x")
    for sp in ("20261021_20261022", "20261024_20261024"):
        totals = out / "events" / sp / "totals"
        totals.mkdir(parents=True)
        (totals / f"ppt_800m_total_{sp}.tif").write_bytes(b"x")
        (totals / f"ppt_800m_uncertainty_{sp}.json").write_text(json.dumps({"volume_m3": 1, "range_m3": [1, 1]}))
    (out / "events" / "20261021_20261022" / "totals" / "rain_mm_20261021_20261022.tif").write_bytes(b"x")  # other naming
    events, other = plan_events(discover(out))
    assert [e["label"] for e in events] == ["PRISM days Oct 21–22, 2026", "PRISM days Oct 24, 2026"]
    assert events[0]["rasters"]["rain"].name == "rain_mm_20261021_20261022.tif" and other == []


def test_write_layer_names_layers_by_window(tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point
    from build_qgis_project import gpkg_layers, gpkg_window
    from validate_stations import write_layer
    gpkg = tmp_path / "v.gpkg"
    for start in ("2026-10-21", "2026-10-24"):
        write_layer(gpd.GeoDataFrame([{"site": "1", "prism_start": start, "prism_end": start}],
                                     geometry=[Point(0, 0)], crs=4269), gpkg, "gauges", window=(start, start))
    assert gpkg_layers(gpkg) == {"gauges_20261021_20261021", "gauges_20261024_20261024"}  # no "latest" copy
    assert gpkg_window(gpkg, "gauges_20261024_20261024") == (dt.date(2026, 10, 24), dt.date(2026, 10, 24))
    import sqlite3
    styled = {r[0] for r in sqlite3.connect(gpkg).execute("SELECT f_table_name FROM layer_styles")}
    assert styled == gpkg_layers(gpkg)  # every copy carries the style


def test_build_real_project_with_two_storms(tmp_path):
    import subprocess
    import zipfile
    from build_qgis_project import find_qgis_python
    if not find_qgis_python()[0]:
        pytest.skip("QGIS not installed")
    out = two_storm_folder(tmp_path)
    res = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "build_qgis_project.py"),
                          "--out", str(out)], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Storm windows: first — Oct 21–22, 2026; second — Oct 24, 2026" in res.stdout
    with zipfile.ZipFile(out / "qgis" / "Two_Storms.qgz") as z:
        qgs = z.read(next(n for n in z.namelist() if n.endswith(".qgs"))).decode()
    for text in ("Storm: first — Oct 21–22, 2026", "Storm: second — Oct 24, 2026", "Other windows",
                 "Precipitation depth (mm), Oct 21–22, 2026", "Water volume per cell (m³), Oct 24, 2026",
                 "Basin total: 0.03 km³", "Basin total: 0.02 km³", "first: Storm total", "second: Validation",
                 "layername=stations_20261024_20261024", "layername=snotel_20261021_20261022",
                 "Boundaries and dams"):
        assert text in qgs, text
    groups = re.findall(r'<layer-tree-group[^>]* name="([^"]+)"', qgs)
    assert groups.index("Validation") < groups.index("Totals")  # points draw above the total rasters
