"""QC checks (pass and fail case for each) and the run manifest, offline."""
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
from rasterio.transform import from_origin
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import manifest  # noqa: E402
import qc  # noqa: E402
from test_offline import SCRIPT, make_zip, zips  # noqa: E402

D1, D2 = dt.date(2026, 10, 21), dt.date(2026, 10, 22)


def test_completeness():
    assert qc.completeness([D1, D2], [D1, D2])["level"] == "pass"
    r = qc.completeness([D1, D2], [D1])
    assert r["level"] == "fail" and r["missing"] == ["2026-10-22"]


def test_download_integrity():
    ok = {"date": D1, "sha256": "ab", "error": ""}
    assert qc.download_integrity([ok])["level"] == "pass"
    r = qc.download_integrity([ok, {"date": D2, "error": "not downloaded (200) You have exceeded the limit"}])
    assert r["level"] == "fail" and "exceeded the limit" in r["detail"]  # PRISM's text verbatim


def test_grid_consistency():
    t = from_origin(-108.5, 37.8, 1 / 120, 1 / 120)
    assert qc.grid_consistency([(D1, t, (10, 10), "W"), (D2, t, (10, 10), "W")])["level"] == "pass"
    assert qc.grid_consistency([(D1, t, (10, 10), "W"), (D2, t, (10, 11), "W")])["level"] == "fail"


def test_nodata_inside():
    assert qc.nodata_inside({"d1": 0.0, "d2": 0.0})["level"] == "pass"
    assert qc.nodata_inside({"d1": 0.0}, constant_pct=14.0)["level"] == "warn"  # e.g. basin part in Canada
    assert qc.nodata_inside({"d1": 0.3})["level"] == "warn"
    assert qc.nodata_inside({"d1": 0.0, "d2": 2.0})["level"] == "fail"


def test_physical_limits():
    assert qc.physical_limits({"d1": 0.0}, {"d1": 120.0})["level"] == "pass"
    assert qc.physical_limits({"d1": 0.0}, {"d1": 450.0})["level"] == "warn"
    assert qc.physical_limits({"d1": -1.0}, {"d1": 10.0})["level"] == "fail"


def test_totals_reconcile():
    assert qc.totals_reconcile([("a = b", 1000.0, 1000.05)])["level"] == "pass"
    assert qc.totals_reconcile([("a = b", 1000.0, 1001.0)])["level"] == "fail"
    assert qc.totals_reconcile([])["level"] == "skip"


def test_release_mix():
    assert qc.release_mix({"d1": 8, "d2": 8})["level"] == "pass"
    assert qc.release_mix({"d1": 5, "d2": 5})["level"] == "pass"
    assert qc.release_mix({"d1": 5, "d2": 6})["level"] == "warn"  # mixed
    assert qc.release_mix({"d1": "2", "d2": "2"})["level"] == "warn"  # early
    assert qc.release_mix({"d1": ""})["level"] == "warn"  # unknown


def test_station_density():
    assert qc.station_density({"d1": 120, "d2": 116}, 64558, 10)["level"] == "pass"
    r = qc.station_density({"d1": 3}, 10000, 10)
    assert r["level"] == "warn" and r["min_stations"] == 3
    assert qc.station_density({}, 10000, 10)["level"] == "skip"


def test_boundary_agreement():
    assert qc.boundary_agreement(64558.0, 64569.1)["level"] == "pass"
    assert qc.boundary_agreement(95.0, 100.0)["level"] == "warn"


def test_strict_and_report():
    checks = [qc.result("a", "pass", "ok"), qc.result("b", "warn", "hm")]
    assert qc.worst_level(checks) == "warn"
    assert qc.worst_level(qc.apply_strict(checks)) == "fail"
    assert qc.report(checks)[0] == "QC: WARN"


def test_manifest_appends_runs(tmp_path):
    args = SimpleNamespace(out=str(tmp_path), start="2026-10-21")
    for i in range(2):
        entry = manifest.start_run("storm_total.py", args)
        entry["qc"] = []
        manifest.append_run(tmp_path, entry)
    runs = json.loads((tmp_path / "run" / "run_manifest.json").read_text())["runs"]
    assert len(runs) == 2
    assert {"script", "command", "args", "started", "finished", "git_commit", "versions"} <= set(runs[0])
    assert {"python", "rasterio", "gdal", "proj", "xarray", "netCDF4"} <= set(runs[0]["versions"])
    assert manifest.info_fields("PRISM_DATASET_RELEASE_NUMBER: 2\nPRISM_DATASET_VERSION: D2\n") == {
        "PRISM_DATASET_RELEASE_NUMBER": "2", "PRISM_DATASET_VERSION": "D2"}


def test_pipeline_manifest_and_corrupt_zip(tmp_path):
    """A run records each day in the manifest; a corrupt cached zip fails QC (exit 1) and is set aside."""
    out = tmp_path / "run"
    raw = zips(out)
    make_zip(raw, "20261021", 10.0)
    make_zip(raw, "20261022", 30.0)
    boundary = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs="EPSG:4326").to_file(boundary)
    cmd = [sys.executable, str(SCRIPT), "--start", "2026-10-21", "--end", "2026-10-22",
           "--boundary", str(boundary), "--out", str(out), "--pause", "0"]
    res = subprocess.run(cmd, check=True, capture_output=True, text=True)
    assert "QC: WARN" in res.stdout and "release_mix" in res.stdout  # release 2 days
    run = json.loads((out / "run" / "run_manifest.json").read_text())["runs"][-1]
    assert {"basin", "window", "days", "qc", "outputs"} <= set(run)
    assert run["basin"]["sha256"] == manifest.sha256(boundary)
    day = run["days"][0]
    assert day["release"] == "2" and len(day["sha256"]) == 64 and day["info"]["PRISM_DATASET_VERSION"] == "D2"
    assert any(o["path"] == "daily/ppt_800m_daily_clip.nc" for o in run["outputs"])

    strict = subprocess.run(cmd + ["--qc-strict"], capture_output=True, text=True)
    assert strict.returncode == 1 and "QC: FAIL" in strict.stdout

    (raw / "prism_ppt_us_30s_20261022.zip").write_bytes(b"not a zip")
    bad = subprocess.run(cmd[:5] + ["2026-10-22"] + cmd[6:], capture_output=True, text=True)
    assert bad.returncode == 1 and "download_integrity" in bad.stdout and "not a valid zip" in bad.stdout
    assert (raw / "prism_ppt_us_30s_20261022.zip.bad").exists()
    assert len(json.loads((out / "run" / "run_manifest.json").read_text())["runs"]) == 3


def test_manifest_skips_hash_for_huge_files(tmp_path, monkeypatch):
    small, big = tmp_path / "a.nc", tmp_path / "b.nc"
    small.write_bytes(b"x" * 10)
    big.write_bytes(b"x" * 100)
    monkeypatch.setattr(manifest, "HASH_LIMIT_BYTES", 50)
    rows = {r["path"]: r for r in manifest.files_entry([small, big, tmp_path / "missing.nc"], tmp_path)}
    assert set(rows) == {"a.nc", "b.nc"} and "sha256" in rows["a.nc"]
    assert "sha256" not in rows["b.nc"] and rows["b.nc"]["bytes"] == 100 and "modified" in rows["b.nc"]
