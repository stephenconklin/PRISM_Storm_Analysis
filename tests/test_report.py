"""Storm report: helpers and an end-to-end run on a synthetic folder (offline)."""
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from report import pick_gauges, standalone, ticks, top_rounded_bar  # noqa: E402
from test_offline import OFFLINE, SCRIPT, make_zip, zips  # noqa: E402


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


@pytest.fixture(scope="module")
def run_folder(tmp_path_factory):
    """A synthetic folder with a cube and a storm_total.py run, but no validation (so some sections are missing)."""
    tmp_path = tmp_path_factory.mktemp("report")
    out = tmp_path / "run"
    for ymd, v in (("20261021", 10.0), ("20261022", 30.0)):
        make_zip(zips(out), ymd, v)
    boundary = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs=4269).to_file(boundary)
    subprocess.run([sys.executable, str(SCRIPT), "--start", "2026-10-21", "--end", "2026-10-22", "--boundary",
                    str(boundary), "--out", str(out), "--pause", "0"], check=True, capture_output=True, env=OFFLINE)
    subprocess.run([sys.executable, str(SCRIPTS / "storm_total.py"), "--out", str(out)], check=True,
                   capture_output=True)
    return out


def report(out, *args):
    res = subprocess.run([sys.executable, str(SCRIPTS / "report.py"), "--out", str(out), "--title", "Test basin", *args],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    return res


def rep(out):
    """The report folder of the test window (no storm.toml, so the folder is named for the window)."""
    return out / "events" / "20261021_20261022" / "report"


SECTION_IDS = ("timeline", "gauges", "runoff", "uncertainty", "snow")


def test_report_end_to_end(run_folder):
    out = run_folder
    report(out)
    page = (rep(out) / "storm_report_20261021_20261022.html").read_text()
    assert "Storm timeline" in page
    assert "30 mm" in page                                     # the peak day is labeled
    assert "Not shown: run validate_stations.py" in page        # missing validation is explained, not an error
    assert "Volume and its uncertainty" in page and "Total range" in page
    assert (rep(out) / "figures" / "timeline_20261021_20261022.svg").exists()
    assert 'src="http' not in page and "<link" not in page and "@import" not in page  # no external resources


def test_report_sections_have_ids_and_title_has_dates(run_folder):
    report(run_folder)
    page = (rep(run_folder) / "storm_report_20261021_20261022.html").read_text()
    for id_ in SECTION_IDS:
        assert f'<h2 id="{id_}">' in page, id_
    # a "Not shown" section keeps its anchor
    assert '<h2 id="gauges">PRISM vs rain gauges</h2><p class="note">Not shown:' in page
    assert "<title>Test basin storm report, PRISM days Oct 21 – Oct 22, 2026</title>" in page
    assert "h2[id]{scroll-margin-top:16px}" in page


def test_web_report_has_no_local_paths_or_operator_notes(run_folder, tmp_path):
    out = run_folder
    local = (rep(out) / "storm_report_20261021_20261022.html")
    report(out)
    before = local.read_text()
    res = report(out, "--web", "--web-out", str(tmp_path / "site"))
    page = (tmp_path / "site" / "storm_report_20261021_20261022.html").read_text()
    for path in (str(out.resolve()), str(out), str(tmp_path), "/Volumes/", "/Users/", "/private/", "/home/"):
        assert path not in page, path
    assert ".py" not in page and "roadmap" not in page and "report/figures" not in page
    assert "Not shown: this check hasn't been run for these days." in page
    assert '<a href="https://github.com/stephenconklin/PRISM_Storm_Analysis">PRISM Storm Analysis</a>' in page
    assert "/blob/main/docs/UNCERTAINTY.md\">how the range is built</a>" in page
    assert "not built yet" in page
    for id_ in SECTION_IDS:
        assert f'<h2 id="{id_}">' in page
    assert 'class="back"' not in page
    assert local.read_text() == before                           # the local report is left alone
    assert "Web report:" in res.stdout
    # without --web-out the page goes to the window's report/web/
    report(out, "--web")
    assert (rep(out) / "web" / "storm_report_20261021_20261022.html").exists()


def test_web_report_back_link(run_folder, tmp_path):
    report(run_folder, "--web", "--web-out", str(tmp_path), "--back-url", "/projects/prism-storm-analysis/",
           "--back-label", "PRISM Storm Analysis")
    page = (tmp_path / "storm_report_20261021_20261022.html").read_text()
    assert ('<p class="back"><a href="/projects/prism-storm-analysis/">&larr; PRISM Storm Analysis</a></p>'
            '<h1>Test basin</h1>') in page


def test_reader_detail():
    from report import reader_detail
    old = "comparison with other precipitation products (roadmap 1.3) not built"
    assert reader_detail(old) == "comparison with other precipitation products not built yet"
    gauges = "no station comparison for this window (run validate_stations.py)"
    assert reader_detail(gauges) == gauges
    assert reader_detail(gauges, web=True) == "no station comparison for this window"
