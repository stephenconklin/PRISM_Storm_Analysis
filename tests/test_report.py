"""Storm report: helpers and an end-to-end run on a synthetic folder (offline)."""
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from report import pick_gauges, standalone, ticks, top_rounded_bar  # noqa: E402
from test_offline import OFFLINE, SCRIPT, make_zip  # noqa: E402


def test_ticks_and_bars():
    t, lo, hi = ticks(0, 31.4, 4)
    assert lo == 0 and hi >= 31.4 and t == sorted(t) and len(t) <= 6
    assert top_rounded_bar(0, 0, 10, 0) == "" and top_rounded_bar(0, 10, 20, 30).startswith("M0.0,40.0")


def test_pick_gauges_prefers_runoff_and_skips_regulated():
    rows = [{"site": "dry", "status": "ok", "regulation": "unregulated", "runoff_const_m3": "1e5", "area_km2": "9000"},
            {"site": "main", "status": "ok", "regulation": "unregulated", "runoff_const_m3": "5e7", "area_km2": "700"},
            {"site": "dam", "status": "ok", "regulation": "regulated", "runoff_const_m3": "9e8", "area_km2": "8000"},
            {"site": "gap", "status": "discharge gap", "regulation": "", "runoff_const_m3": "", "area_km2": "100"}]
    assert [g["site"] for g in pick_gauges(rows, n=2)] == ["main", "dry"]
    assert [g["site"] for g in pick_gauges(rows, ["dam"])] == ["dam"]


def test_standalone_svg_has_colors():
    s = standalone('<svg viewBox="0 0 10 10" xmlns="http://www.w3.org/2000/svg"><rect class="f1"/></svg>')
    assert s.startswith("<?xml") and "--s1:#2a78d6" in s and ".f1{fill:var(--s1)}" in s


def test_report_end_to_end(tmp_path):
    out = tmp_path / "run"
    (out / "raw_zips").mkdir(parents=True)
    for ymd, v in (("20261021", 10.0), ("20261022", 30.0)):
        make_zip(out / "raw_zips", ymd, v)
    boundary = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs=4269).to_file(boundary)
    subprocess.run([sys.executable, str(SCRIPT), "--start", "2026-10-21", "--end", "2026-10-22", "--boundary",
                    str(boundary), "--out", str(out), "--pause", "0"], check=True, capture_output=True, env=OFFLINE)
    subprocess.run([sys.executable, str(SCRIPTS / "storm_total.py"), "--out", str(out)], check=True,
                   capture_output=True)
    res = subprocess.run([sys.executable, str(SCRIPTS / "report.py"), "--out", str(out), "--title", "Test basin"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    page = (out / "report" / "storm_report_20261021_20261022.html").read_text()
    assert "<title>Test basin storm report</title>" in page and "Storm timeline" in page
    assert "30 mm" in page                                     # the peak day is labeled
    assert "Not shown: run validate_stations.py" in page        # missing validation is explained, not an error
    assert "Volume and its uncertainty" in page and "Total range" in page
    assert (out / "report" / "figures" / "timeline_20261021_20261022.svg").exists()
    assert 'src="http' not in page and "<link" not in page and "@import" not in page  # no external resources
