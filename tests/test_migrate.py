"""pipeline.py migrate: a folder in the old layout (everything at the top) moves to the current one offline,
by renaming, without losing anything and without making finished pipeline steps run again."""
import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point, box

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from test_offline import OFFLINE, make_zip  # noqa: E402
from validate_stations import write_layer  # noqa: E402

PIPELINE = SCRIPTS / "pipeline.py"
W = ("2026-10-21", "2026-10-22")
SP = "20261021_20261022"


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else ""


def old_fingerprint(out, name, args, stations_summary=None):
    """A step's fingerprint as the pipeline computed it in the old layout."""
    h = hashlib.sha256(json.dumps([name, args]).encode())
    h.update(file_sha(out / "storm.toml").encode())
    for var in ("ppt", "tmean"):
        h.update(file_sha(out / f"{var}_800m_release_history.csv").encode())
    if stations_summary:
        h.update(file_sha(stations_summary).encode())
    return h.hexdigest()[:16]


def legacy_folder(root, name="Basin", release=2):
    """An old-layout folder with every kind of file the real folders had."""
    out = root / name
    (out / "raw_zips" / "releases").mkdir(parents=True)
    make_zip(out / "raw_zips", "20261021", 10.0, release=release)
    make_zip(root, "20261021", 9.0, release=1)
    (root / "prism_ppt_us_30s_20261021.zip").rename(out / "raw_zips" / "releases" / "prism_ppt_us_30s_20261021_r1.zip")
    (out / "raw_zips" / "prism_ppt_us_30s_20261022.zip.part").write_bytes(b"half")
    ext = out / "extracted" / "prism_ppt_us_30s_20261021"
    ext.mkdir(parents=True)
    with zipfile.ZipFile(out / "raw_zips" / "prism_ppt_us_30s_20261021.zip") as z:
        z.extractall(ext)
    (out / "extracted" / ".unzip-x").mkdir()
    (out / "normals_zips").mkdir()
    (out / "normals_zips" / "prism_ppt_us_30s_202010_avg_30y.zip").write_bytes(b"normal")
    (out / "normals" / "prism_ppt_us_30s_202010_avg_30y").mkdir(parents=True)
    (out / "normals" / "prism_ppt_us_30s_202010_avg_30y" / "x.tif").write_bytes(b"n")
    s = {"name": "Test", "boundary": "boundary.geojson", "res": "800m",
         "download": {"start": dt.date(2026, 10, 20), "end": dt.date(2026, 10, 23), "vars": ["ppt"]},
         "events": [{"name": "storm", "start": dt.date(2026, 10, 21), "end": dt.date(2026, 10, 22)}]}
    poly = box(-108.4, 37.5, -108.0, 37.7)
    gpd.GeoDataFrame(geometry=[poly], crs=4269).to_file(out / "boundary.geojson")
    (out / "storm.toml").write_text(config.render(s))
    (out / "clipped").mkdir()
    (out / "clipped" / "ppt_800m_20261021_clip.tif").write_bytes(b"clip")
    (out / "clipped" / "ppt_800m_20261021_clip.tif.aux.xml").write_text("<x/>")
    for f in ("ppt_800m_daily_clip.nc", "ppt_800m_daily_clip.nc.aux.xml", "daily_basin_summary.csv",
              "ppt_800m_release_history.csv", "ppt_800m_total_20261020_20261023.tif",  # the download window's
              f"ppt_800m_total_{SP}.tif", f"ppt_800m_total_{SP}.tif.aux.xml", f"ppt_800m_volume_{SP}.tif",
              f"ppt_800m_uncertainty_{SP}.json", f"ppt_800m_volume_m3_{SP}.nc",
              "ppt_800m_total_20261028_20261031.tif"):  # a window that is no event any more
        (out / f).write_text(f)
    v = out / "validation"
    v.mkdir()
    summary = json.dumps({"window": {"start": W[0], "end": W[1]}})
    (v / "stations_summary.json").write_text(summary)
    (v / f"stations_summary_{SP}.json").write_text(summary)         # identical "latest" copy: dropped
    (v / "stations.csv").write_text("station\nA\n")                  # only the latest: named for its window
    (v / "runoff_summary.csv").write_text(f"site,prism_start,prism_end\nX,{W[0]},{W[1]}\n")
    (v / "runoff_X.json").write_text(json.dumps({"run_key": {"start": W[0], "end": W[1]}, "n": 2}))
    (v / f"runoff_X_{SP}.json").write_text(json.dumps({"run_key": {"start": W[0], "end": W[1]}, "n": 1}))  # differs
    (v / "runoff_X_flow.csv").write_text("time_utc,q_cfs\n")
    gpd.GeoDataFrame(geometry=[poly], crs=4269).to_file(v / "runoff_X_basin.geojson")
    gpkg = v / "validation.gpkg"
    write_layer(gpd.GeoDataFrame([{"station": "A"}], geometry=[Point(-108.2, 37.6)], crs=4269), gpkg, "stations")
    write_layer(gpd.GeoDataFrame([{"site": "X", "prism_start": W[0], "prism_end": W[1]}],
                                 geometry=[Point(-108.2, 37.6)], crs=4269), gpkg, "gauges", window=W)
    write_layer(gpd.GeoDataFrame([{"site": "X", "prism_start": W[0], "prism_end": W[1]}],
                                 geometry=[Point(-108.2, 37.6)], crs=4269), gpkg, "gauges")  # the latest
    write_layer(gpd.GeoDataFrame([{"name": "Dam"}], geometry=[Point(-108.1, 37.6)], crs=4269), gpkg, "dams")
    write_layer(gpd.GeoDataFrame([{"name": "B", "prism_start": W[0], "prism_end": W[1]}], geometry=[poly], crs=4269),
                out / "totals.gpkg", "basin_total")
    (out / "subbasin_totals.csv").write_text(f"huc,prism_start,prism_end\n1,{W[0]},{W[1]}\n")
    r = out / "report"
    for f in (f"storm_report_{SP}.html", f"storm_report_{SP}_web.html", f"figures/timeline_{SP}.svg",
              f"history/storm_report_{SP}_20261105-172638.html", f"web/storm_report_{SP}.html"):
        (r / f).parent.mkdir(parents=True, exist_ok=True)
        (r / f).write_text(f)
    (out / "QGIS").mkdir()
    (out / "QGIS" / f"{name}.qgz").write_bytes(b"qgz")
    (out / "QGIS" / f"{name}.qgz.bak").write_bytes(b"older")
    (out / "logs" / "20261105-163619").mkdir(parents=True)
    (out / "logs" / "20261105-163619-pipeline.log").write_text("log")
    (out / "run_manifest.json").write_text(json.dumps({"runs": [{"script": "storm_total.py"}]}))
    (out / "notes.txt").write_text("mine")
    (out / ".lock").write_text(json.dumps({"pid": 999999, "host": os.uname().nodename}))  # a run that is gone
    tif = lambda kind: str(out / f"ppt_800m_{kind}_{SP}.tif")  # noqa: E731
    totals_args = ["--start", W[0], "--end", W[1], "--tif", tif("total"), "--volume-tif", tif("volume")]
    steps = {"subbasins:storm": {"status": "ok", "log": "logs/20261105-163619/05-subbasins-storm.log",
                                 "fingerprint": old_fingerprint(out, "subbasins", ["--start", W[0], "--end", W[1]])},
             "totals:storm": {"status": "ok", "fingerprint": old_fingerprint(
                 out, "totals", totals_args, v / f"stations_summary_{SP}.json")},
             "snow:storm": {"status": "ok", "fingerprint": "out-of-date"}}
    (out / "pipeline_state.json").write_text(json.dumps({"steps": steps}))
    return out


def migrate(out, *extra):
    return subprocess.run([sys.executable, str(PIPELINE), "migrate", "--out", str(out), "--yes", "--no-qgis", *extra],
                          capture_output=True, text=True, env=OFFLINE)


def test_migrate_moves_everything_and_keeps_finished_steps(tmp_path):
    out = legacy_folder(tmp_path)
    blocked = subprocess.run([sys.executable, str(SCRIPTS / "storm_total.py"), "--out", str(out)],
                             capture_output=True, text=True)
    assert blocked.returncode == 2 and "pipeline.py migrate" in blocked.stderr  # scripts refuse the old layout

    before = sorted(p.relative_to(out) for p in out.rglob("*"))
    dry = migrate(out, "--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "Dry run: nothing was changed" in dry.stdout and "notes.txt" in dry.stdout
    assert sorted(p.relative_to(out) for p in out.rglob("*")) == before  # nothing moved

    res = migrate(out)
    assert res.returncode == 0, res.stdout + res.stderr
    store, ev = tmp_path / "prism_store", out / "events" / "storm"
    assert {p.name for p in out.iterdir()} == {"storm.toml", "basin", "daily", "events", "qgis", "run", "notes.txt",
                                              "boundary.geojson"}  # storm.toml names it: it stays, copied to basin/
    assert json.loads((ev / "event.json").read_text())["name"] == "storm"
    # basin and daily
    assert (out / "basin" / "boundary.geojson").exists() and (out / "basin" / "gauges" / "X_basin.geojson").exists()
    for f in ("clipped/ppt_800m_20261021_clip.tif", "clipped/ppt_800m_20261021_clip.tif.aux.xml",
              "ppt_800m_daily_clip.nc", "ppt_800m_daily_clip.nc.aux.xml", "daily_basin_summary.csv",
              "ppt_800m_release_history.csv", "ppt_800m_total_20261020_20261023.tif"):
        assert (out / "daily" / f).exists(), f
    # the event
    for f in (f"ppt_800m_total_{SP}.tif", f"ppt_800m_total_{SP}.tif.aux.xml", f"ppt_800m_volume_{SP}.tif",
              f"ppt_800m_uncertainty_{SP}.json", f"ppt_800m_volume_m3_{SP}.nc"):
        assert (ev / "totals" / f).exists(), f
    assert (out / "events" / "20261028_20261031" / "totals" / "ppt_800m_total_20261028_20261031.tif").exists()
    assert (ev / "subbasins" / f"subbasin_totals_{SP}.csv").exists()
    vd = ev / "validation"
    assert {p.name for p in vd.iterdir()} == {f"stations_summary_{SP}.json", f"stations_{SP}.csv",
                                             f"runoff_summary_{SP}.csv", "validation.gpkg", "runoff"}
    assert {p.name for p in (vd / "runoff").iterdir()} == {f"runoff_X_{SP}.json", f"runoff_X_flow_{SP}.csv"}
    assert json.loads((vd / "runoff" / f"runoff_X_{SP}.json").read_text())["n"] == 1  # the window's own copy
    from build_qgis_project import gpkg_layers
    assert gpkg_layers(vd / "validation.gpkg") == {f"stations_{SP}", f"gauges_{SP}"}
    assert gpkg_layers(ev / "subbasins" / "totals.gpkg") == {f"basin_total_{SP}"}
    assert gpkg_layers(out / "basin" / "dams.gpkg") == {"dams"}
    styled = {r[0] for r in sqlite3.connect(vd / "validation.gpkg").execute("SELECT f_table_name FROM layer_styles")}
    assert styled == {f"stations_{SP}", f"gauges_{SP}"}
    rep = ev / "report"
    for f in (f"storm_report_{SP}.html", f"web/storm_report_{SP}.html", f"figures/timeline_{SP}.svg",
              f"history/storm_report_{SP}_20261105-172638.html"):
        assert (rep / f).exists(), f
    # kept aside, never lost
    aside = out / "run" / "pre_migration"
    assert (aside / "validation" / "runoff_X.json").exists()  # differed from the window's copy
    assert (aside / "report" / "web" / f"storm_report_{SP}.html").exists()  # a second web copy of the report
    assert (aside / "validation" / "validation.gpkg").exists() and (aside / "totals.gpkg").exists()
    # run/ and qgis/
    assert (out / "qgis" / "Basin.qgz").read_bytes() == b"qgz" and "QGIS" not in os.listdir(out)  # exact name
    assert (out / "run" / "logs" / "20261105-163619-pipeline.log").exists()
    runs = json.loads((out / "run" / "run_manifest.json").read_text())["runs"]
    assert runs[0] == {"script": "storm_total.py"} and runs[-1]["migration"]["layout"] == 2
    state = json.loads((out / "run" / "pipeline_state.json").read_text())["steps"]
    assert state["subbasins:storm"]["log"].startswith("run/logs/")
    import pipeline
    c = pipeline.Ctx(out)
    for key, step in (("subbasins:storm", "subbasins"), ("totals:storm", "totals")):
        s = next(x for x in pipeline.STEPS if x.name == step)
        assert state[key]["fingerprint"] == pipeline.fingerprint(c, s, s.args(c, c.events[0])), key  # still done
    assert state["snow:storm"]["fingerprint"] == "out-of-date"  # was out of date: stays so
    # the store
    z = store / "zips" / "us" / "800m" / "ppt"
    assert (z / "prism_ppt_us_30s_20261021.zip").exists() and (z / "releases" / "prism_ppt_us_30s_20261021_r1.zip").exists()
    assert (store / "extracted" / "us" / "800m" / "ppt" / "prism_ppt_us_30s_20261021_r2").is_dir()
    assert (store / "normals" / "us" / "800m" / "ppt" / "prism_ppt_us_30s_202010_avg_30y.zip").exists()
    assert (store / "normals" / "us" / "800m" / "ppt" / "prism_ppt_us_30s_202010_avg_30y" / "x.tif").exists()
    assert not list(out.rglob("*.part")) and not list(store.rglob(".unzip-*"))
    assert "notes.txt" in res.stdout  # not recognized: left alone and listed

    again = migrate(out)
    assert again.returncode == 0 and "already uses the current folder layout" in again.stdout
    (out / "daily" / "ppt_800m_daily_clip.nc").unlink()  # a placeholder, not a real cube
    status = subprocess.run([sys.executable, str(PIPELINE), "status", "--out", str(out), "--offline"],
                            capture_output=True, text=True, env=OFFLINE)
    assert status.returncode == 0, status.stdout + status.stderr


def test_rebuilding_the_project_keeps_the_older_backup(tmp_path, monkeypatch):
    import build_qgis_project
    import migrate as m
    out = legacy_folder(tmp_path)
    assert migrate(out).returncode == 0
    calls = []
    monkeypatch.setattr(build_qgis_project, "find_qgis_python", lambda explicit=None: ("qgis-python", ""))
    monkeypatch.setattr(m.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or
                        type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    assert m.rebuild_qgis(out).startswith("QGIS project rebuilt") and calls
    assert (out / "run" / "pre_migration" / "qgis" / "Basin.qgz.bak").read_bytes() == b"older"
    assert not (out / "qgis" / "Basin.qgz.bak").exists()  # the builder makes a new one from Basin.qgz


def test_migrate_shares_the_store_between_basins(tmp_path):
    """A second basin with the same zip adds nothing to the store; one with an older release of a day archives it."""
    a = legacy_folder(tmp_path, "A")
    assert migrate(a).returncode == 0
    b = legacy_folder(tmp_path, "B")               # the same zips
    c = legacy_folder(tmp_path, "C", release=3)    # a newer release of Oct 21
    for out in (b, c):
        res = migrate(out)
        assert res.returncode == 0, res.stdout + res.stderr
    from layout import zip_release
    z = tmp_path / "prism_store" / "zips" / "us" / "800m" / "ppt"
    assert zip_release(z / "prism_ppt_us_30s_20261021.zip") == "3"
    assert {p.name for p in (z / "releases").iterdir()} == {"prism_ppt_us_30s_20261021_r1.zip",
                                                           "prism_ppt_us_30s_20261021_r2.zip"}
    ext = tmp_path / "prism_store" / "extracted" / "us" / "800m" / "ppt"
    assert {p.name for p in ext.iterdir()} == {"prism_ppt_us_30s_20261021_r2", "prism_ppt_us_30s_20261021_r3"}
