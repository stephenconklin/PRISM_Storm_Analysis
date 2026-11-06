"""Offline tests for the pipeline, settings, run-time support (lock, atomic writes, exit codes, logs), resume after
an interruption, the shared zip cache and HTTP retries."""
import csv
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
import requests
from shapely.geometry import box
import xarray as xr

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import manifest  # noqa: E402
import runtime  # noqa: E402
from test_offline import OFFLINE, SCRIPT, FakeResponse, FakeSession, make_zip  # noqa: E402

FAST = {**OFFLINE, "PRISM_STORM_RETRY_WAIT": "0"}
PIPELINE = SCRIPTS / "pipeline.py"


def basin(tmp_path):
    path = tmp_path / "basin.geojson"
    gpd.GeoDataFrame(geometry=[box(-108.4, 37.5, -108.0, 37.7)], crs="EPSG:4326").to_file(path)
    return path


def seed(raw, days, var="ppt", release=2, value=None):
    raw.mkdir(parents=True, exist_ok=True)
    for i, ymd in enumerate(days):
        make_zip(raw, ymd, float(i * 10) if value is None else value, release=release, var=var)


def watershed(out, boundary, start, end, *extra, env=FAST):
    return subprocess.run([sys.executable, str(SCRIPT), "--start", start, "--end", end, "--boundary", str(boundary),
                           "--out", str(out), "--pause", "0", *extra], capture_output=True, text=True, env=env)


# ---------- runtime ----------

def test_atomic_writes(tmp_path):
    p = tmp_path / "a.json"
    runtime.write_json(p, {"x": 1})
    assert json.loads(p.read_text()) == {"x": 1} and not list(tmp_path.glob("*.part"))
    with pytest.raises(RuntimeError):
        with runtime.atomic_open(p, "w") as f:
            f.write("half")
            raise RuntimeError("interrupted")
    assert json.loads(p.read_text()) == {"x": 1} and not list(tmp_path.glob("*.part"))  # old file untouched


def test_folder_lock(tmp_path, monkeypatch):
    monkeypatch.delenv(runtime.LOCK_ENV, raising=False)
    lock = runtime.FolderLock(tmp_path).acquire()
    with pytest.raises(runtime.Stop) as e:
        runtime.FolderLock(tmp_path).acquire()  # same live process holds it
    assert e.value.code == runtime.EXIT_USAGE
    monkeypatch.setenv(runtime.LOCK_ENV, str(tmp_path / ".lock"))
    runtime.FolderLock(tmp_path).acquire()  # a pipeline step shares its pipeline's lock
    monkeypatch.delenv(runtime.LOCK_ENV)
    lock.release()
    assert not (tmp_path / ".lock").exists()
    (tmp_path / ".lock").write_text(json.dumps({"pid": 999999, "host": os.uname().nodename}))  # a run that is gone
    runtime.FolderLock(tmp_path).acquire().release()


@pytest.mark.parametrize("error, code, status", [(KeyboardInterrupt, 130, "interrupted"),
                                                 (requests.ConnectionError("down"), 4, "network_error"),
                                                 (ValueError("bug"), 5, "error")])
def test_run_main_records_failed_runs(tmp_path, monkeypatch, error, code, status):
    monkeypatch.delenv(runtime.LOCK_ENV, raising=False)
    monkeypatch.delenv(runtime.LOG_ENV, raising=False)

    def main():
        run = manifest.start_run("x.py", SimpleNamespace(out=str(tmp_path)))
        runtime.attach(tmp_path, run)
        print("working")
        raise error
    with pytest.raises(SystemExit) as e:
        runtime.run_main(main)
    assert e.value.code == code
    run = manifest.read_runs(tmp_path)[-1]
    assert run["status"] == status and not (tmp_path / ".lock").exists()
    assert "working" in (tmp_path / run["log"]).read_text()


def test_manifest_corrupt_file_is_kept(tmp_path):
    (tmp_path / "run_manifest.json").write_text('{"runs": [')
    manifest.append_run(tmp_path, {"script": "x.py"})
    assert len(manifest.read_runs(tmp_path)) == 1 and list(tmp_path.glob("run_manifest.corrupt-*.json"))


# ---------- settings ----------

def settings(tmp_path, boundary, **extra):
    s = {"name": "Test", "boundary": str(boundary), "res": "800m",
         "download": {"start": dt.date(2026, 10, 20), "end": dt.date(2026, 10, 23), "vars": ["ppt", "tmean"]},
         "events": [{"name": "one", "start": dt.date(2026, 10, 21), "end": dt.date(2026, 10, 22)},
                    {"name": "two", "start": dt.date(2026, 10, 23), "end": dt.date(2026, 10, 23)}],
         "rain_snow": {"all_snow_c": -1.0, "all_rain_c": 2.0}, **extra}
    out = tmp_path / "run"
    out.mkdir(exist_ok=True)
    (out / "storm.toml").write_text(config.render(s))
    return out


def test_settings_roundtrip_and_defaults(tmp_path):
    import argparse
    out = settings(tmp_path, basin(tmp_path))
    s = config.load(out)
    assert config.check(s) == [] and config.events(s)[1] == ("two", "2026-10-23", "2026-10-23")

    def download_parser():  # like prism_watershed.py's
        ap = argparse.ArgumentParser()
        ap.add_argument("--out")
        ap.add_argument("--start", required=True)
        ap.add_argument("--end", required=True)
        g = ap.add_mutually_exclusive_group(required=True)
        g.add_argument("--huc")
        g.add_argument("--boundary")
        return ap
    a = config.parse_args(download_parser(), ["--out", str(out)], role="download")
    assert (a.start, a.end, a.huc) == ("2026-10-20", "2026-10-23", None) and a.boundary.endswith("basin.geojson")
    a = config.parse_args(download_parser(), ["--out", str(out), "--huc", "1408", "--end", "2026-10-21"],
                          role="download")
    assert a.huc == "1408" and a.boundary is None and a.end == "2026-10-21"  # flags win, group mates not filled

    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--all-snow-c", type=float, default=0.0)
    a = config.parse_args(ap, ["--out", str(out)])
    assert (a.start, a.end, a.all_snow_c) == ("2026-10-21", "2026-10-22", -1.0)
    a = config.parse_args(ap, ["--out", str(out), "--event", "two"])
    assert (a.start, a.end) == ("2026-10-23", "2026-10-23")

    def runoff_parser():
        ap = argparse.ArgumentParser()
        for opt in ("--out", "--start", "--end"):
            ap.add_argument(opt)
        ap.add_argument("--after-days", type=int, default=7)
        return ap
    s = config.load(out)
    s["events"][0]["after_days"] = 3  # the first event's own flow window
    s.pop("_path")
    (out / "storm.toml").write_text(config.render(s))
    assert config.parse_args(runoff_parser(), ["--out", str(out)]).after_days == 3
    # an event's own settings follow its window, whichever way it is chosen
    assert config.parse_args(runoff_parser(), ["--out", str(out), "--start", "2026-10-23", "--end", "2026-10-23"]).after_days == 7
    assert config.parse_args(runoff_parser(), ["--out", str(out), "--start", "2026-10-21", "--end", "2026-10-22"]).after_days == 3

    (out / "storm.toml").write_text("res = [")
    with pytest.raises(runtime.Stop):
        config.parse_args(ap, ["--out", str(out)])


def test_settings_check():
    s = {"huc": "1408", "download": {"start": dt.date(2026, 10, 21), "end": dt.date(2026, 10, 22)},
         "events": [{"name": "e", "start": dt.date(2026, 10, 20), "end": dt.date(2026, 10, 22)}]}
    assert any("outside the download window" in p for p in config.check(s))
    assert any("exactly one of huc or boundary" in p for p in config.check({**s, "boundary": "x.gpkg"}))


# ---------- pipeline ----------

def test_pipeline_offline(tmp_path):
    """init + run with every network step failing fast: local steps still run, the summary says what failed,
    a rerun skips finished steps, and status reports what is left."""
    days = ["20261020", "20261021", "20261022", "20261023"]
    out = tmp_path / "run"
    seed(out / "raw_zips", days)
    seed(out / "raw_zips", days, var="tmean", value=1.5)
    b = basin(tmp_path)
    init = subprocess.run([sys.executable, str(PIPELINE), "init", "--boundary", str(b), "--out", str(out),
                           "--start", "2026-10-21", "--end", "2026-10-22", "--event", "storm1", "--no-qgis"],
                          capture_output=True, text=True, env=FAST)
    assert init.returncode == 0, init.stderr
    assert config.events(config.load(out)) == [("storm1", "2026-10-21", "2026-10-22")]
    assert config.iso(config.load(out)["download"]["start"]) == "2026-10-20"  # padded by a day

    cmd = [sys.executable, str(PIPELINE), "run", "--out", str(out), "--no-qgis"]
    res = subprocess.run(cmd, capture_output=True, text=True, env=FAST)
    assert res.returncode == runtime.EXIT_INCOMPLETE, res.stdout + res.stderr
    state = json.loads((out / "pipeline_state.json").read_text())["steps"]
    assert state["ppt"]["status"] == state["totals:storm1"]["status"] == state["report:storm1"]["status"] == "ok"
    assert state["runoff:storm1"]["status"] == "network_error"
    for kind in ("total", "volume", "rain", "snow"):
        assert (out / f"ppt_800m_{kind}_20261021_20261022.tif").exists()
    assert json.loads((out / "ppt_800m_uncertainty_20261021_20261022.json").read_text())["rain_snow_ramp_c"] == [0.0, 3.0]
    assert (out / "report" / "storm_report_20261021_20261022.html").exists()
    assert not (out / ".lock").exists() and not list(out.rglob("*.part"))
    assert (out / state["totals:storm1"]["log"]).read_text().count("Volume:") == 1

    again = subprocess.run(cmd, capture_output=True, text=True, env=FAST)
    rows = {line.split()[0]: line for line in again.stdout.splitlines() if line[:10].strip() in
            ("ppt", "tmean", "totals", "runoff")}
    assert "done earlier" in rows["ppt"] and "done earlier" in rows["totals"] and "network_error" in rows["runoff"]

    status = subprocess.run([sys.executable, str(PIPELINE), "status", "--out", str(out), "--offline"],
                            capture_output=True, text=True, env=FAST)
    assert status.returncode == 0 and "4/4 days (r2: 4)" in status.stdout
    assert "runoff failed (network_error)" in status.stdout and "totals ✓" in status.stdout

    statuses = {(r["script"], r["status"]) for r in manifest.read_runs(out)}
    assert ("validate_runoff.py", "network_error") in statuses and ("pipeline.py", "incomplete") in statuses


def test_pipeline_dry_run_lists_commands(tmp_path):
    out = settings(tmp_path, basin(tmp_path))
    res = subprocess.run([sys.executable, str(PIPELINE), "run", "--out", str(out), "--dry-run", "--offline",
                          "--no-qgis", "--event", "two"], capture_output=True, text=True, env=FAST)
    assert res.returncode == 0, res.stderr
    assert "storm_total.py --out" in res.stdout and "--start 2026-10-23" in res.stdout and "one" not in res.stdout
    assert "Disk:" in res.stdout and not (out / "pipeline_state.json").exists()


def test_status_sees_newer_releases_and_stale_outputs(tmp_path, monkeypatch):
    import pipeline
    import prism_watershed
    out = tmp_path / "run"
    seed(out / "raw_zips", ["20261020", "20261021", "20261022", "20261023"])
    b = basin(tmp_path)
    assert watershed(out, b, "2026-10-20", "2026-10-23").returncode == 0
    settings(tmp_path, b, download={"start": dt.date(2026, 10, 20), "end": dt.date(2026, 10, 23), "vars": ["ppt"]})
    entry = {"script": "storm_total.py", "args": {"res": "800m"}, "window": {"start": "2026-10-21", "end": "2026-10-22"},
             "input_releases": {"ppt_800m": [1, 1]}}
    manifest.append_run(out, entry)
    monkeypatch.setattr(prism_watershed, "available_releases",
                        lambda var, res, region, start, end, session: {dt.date(2026, 10, 22): 3})
    c = pipeline.Ctx(out)
    st = pipeline.folder_status(c)
    assert st["vars"]["ppt"]["newer"] == [("2026-10-22", 2, 3)]
    assert st["events"][0]["stale"] == ["totals"]
    assert any("pipeline.py update" in x for x in st["actions"])


# ---------- interruption and resume ----------

def outputs(out):
    with xr.open_dataset(out / "ppt_800m_daily_clip.nc") as ds:
        cube = ds.ppt.values.copy(), list(ds.release.values)
    rows = [(r["date"], r["release"], r["basin_mean_mm"]) for r in csv.DictReader(open(out / "daily_basin_summary.csv"))]
    return cube, rows


DAYS = [f"202610{d:02d}" for d in range(15, 27)]


def test_interrupted_run_resumes(tmp_path):
    """Ctrl-C partway through prism_watershed.py, then the same command again: same outputs as an uninterrupted
    run, the interruption recorded, nothing half-written left behind."""
    b = basin(tmp_path)
    clean, cut = tmp_path / "clean", tmp_path / "cut"
    for out in (clean, cut):
        seed(out / "raw_zips", DAYS)
    assert watershed(clean, b, "2026-10-15", "2026-10-26").returncode == 0
    proc = subprocess.Popen([sys.executable, str(SCRIPT), "--start", "2026-10-15", "--end", "2026-10-26", "--boundary",
                             str(b), "--out", str(cut), "--pause", "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env={**FAST, "PYTHONUNBUFFERED": "1"})  # lines as they are printed
    for line in proc.stdout:
        if "[2/12]" in line:
            proc.send_signal(signal.SIGINT)
            break
    _, err = proc.communicate(timeout=120)
    assert proc.returncode == 130 and "Interrupted." in err and "To continue, rerun" in err
    assert manifest.read_runs(cut)[-1]["status"] == "interrupted"
    assert len(list((cut / "clipped").glob("*.tif"))) < len(DAYS)
    assert not (cut / ".lock").exists() and not list(cut.rglob("*.part"))
    assert watershed(cut, b, "2026-10-15", "2026-10-26").returncode == 0
    (c_cube, c_rel), c_rows = outputs(clean)
    (k_cube, k_rel), k_rows = outputs(cut)
    assert np.array_equal(c_cube, k_cube, equal_nan=True) and c_rel == k_rel and c_rows == k_rows


def test_leftovers_from_a_crash_are_recovered(tmp_path):
    """What a crash could leave (a stale lock, a truncated clip and zip, a half-unzipped folder, .part files)
    doesn't stop or corrupt the next run."""
    b = basin(tmp_path)
    out = tmp_path / "run"
    seed(out / "raw_zips", DAYS[:3])
    assert watershed(out, b, "2026-10-15", "2026-10-17").returncode == 0
    before = outputs(out)
    (out / ".lock").write_text(json.dumps({"pid": 999999, "host": os.uname().nodename, "command": "gone"}))
    (out / "clipped" / "ppt_800m_20261016_clip.tif").write_bytes(b"II*\x00truncated")
    (out / "extracted" / ".unzip-abc").mkdir()
    (out / "ppt_800m_daily_clip.nc.part").write_bytes(b"half")
    make_zip(tmp_path, "20261017", 99.0)  # a truncated copy of a zip, as a crash mid-download would leave
    (out / "raw_zips" / "prism_ppt_us_30s_20261017.zip").write_bytes(
        (tmp_path / "prism_ppt_us_30s_20261017.zip").read_bytes()[:200])
    res = watershed(out, b, "2026-10-15", "2026-10-17")
    assert "Traceback" not in res.stderr
    assert not (out / "extracted" / ".unzip-abc").exists() and not (out / ".lock").exists()
    assert (out / "raw_zips" / "prism_ppt_us_30s_20261017.zip.bad").exists()  # set aside; downloaded next run
    (out / "raw_zips" / "prism_ppt_us_30s_20261017.zip.bad").unlink()
    seed(out / "raw_zips", DAYS[:3])  # what the next download would bring back
    assert watershed(out, b, "2026-10-15", "2026-10-17").returncode == 0
    after = outputs(out)
    assert np.array_equal(before[0][0], after[0][0], equal_nan=True) and before[1] == after[1]


# ---------- shared cache ----------

def test_shared_cache(tmp_path):
    """Two basins share one zip cache: each folder links the shared zips, and a newer release one basin brought
    in is picked up by the other without a download (its older zip archived)."""
    cache = tmp_path / "cache"
    seed(cache, ["20261021", "20261022"])
    b = basin(tmp_path)
    a_out, b_out = tmp_path / "a", tmp_path / "b"
    for out in (a_out, b_out):
        res = watershed(out, b, "2026-10-21", "2026-10-22", "--cache", str(cache))
        assert res.returncode == 0, res.stdout + res.stderr
        assert "request failed" not in res.stdout
        z = out / "raw_zips" / "prism_ppt_us_30s_20261022.zip"
        assert os.path.samefile(z, cache / z.name)
    tmp = tmp_path / "newer"
    seed(tmp, ["20261022"], release=3, value=33.0)
    (tmp / "prism_ppt_us_30s_20261022.zip").replace(cache / "prism_ppt_us_30s_20261022.zip")  # basin A's refresh
    res = watershed(b_out, b, "2026-10-21", "2026-10-22", "--cache", str(cache))
    assert "release 3 from the shared cache" in res.stdout and "release 2 -> 3" in res.stdout
    assert (b_out / "raw_zips" / "releases" / "prism_ppt_us_30s_20261022_r2.zip").exists()

    own = tmp_path / "own"  # a folder's own zips are added to the cache
    seed(own / "raw_zips", ["20261023"])
    assert watershed(own, b, "2026-10-23", "2026-10-23", "--cache", str(cache)).returncode == 0
    assert (cache / "prism_ppt_us_30s_20261023.zip").exists()


# ---------- network robustness ----------

def test_fetch_zip_retries_a_dropped_connection_once(tmp_path):
    from prism_watershed import fetch_zip, zip_release
    make_zip(tmp_path, "20261022", 5.0)
    content = (tmp_path / "prism_ppt_us_30s_20261022.zip").read_bytes()

    class Flaky(FakeSession):
        def get(self, url, params=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise requests.ConnectionError("reset")
            return FakeResponse(self.content)
    s = Flaky(content)
    target = tmp_path / "out.zip"
    assert fetch_zip("ppt", "800m", "us", dt.date(2026, 10, 22), "", target, s, retry_wait=0) and s.calls == 2
    assert zip_release(target) == "2"

    class Down(FakeSession):
        def get(self, url, params=None, timeout=None):
            self.calls += 1
            raise requests.ConnectionError("down")
    d = Down(b"")
    assert not fetch_zip("ppt", "800m", "us", dt.date(2026, 10, 22), "", tmp_path / "x.zip", d, retry_wait=0)
    assert d.calls == 2 and not (tmp_path / "x.zip").exists()


def test_get_retries_and_never_logs_the_key(tmp_path, monkeypatch):
    import observations
    monkeypatch.setenv("USGS_API_KEY", "SECRET-KEY-123")
    monkeypatch.setattr(observations, "TRANSIENT_WAIT_S", 0)
    calls = []

    class Reply:
        status_code, content, headers, url = 200, b"{}", {}, "https://api.waterdata.usgs.gov/x?f=json"

        def raise_for_status(self):
            pass

    def fake(method, url, params=None, json=None, headers=None, timeout=None):
        calls.append(headers)
        if len(calls) == 1:
            raise requests.Timeout("slow")
        return Reply()
    monkeypatch.setattr(observations.requests, "request", fake)
    log = runtime.start_log(tmp_path, "test.py", verbose=True)
    try:
        observations._get("https://api.waterdata.usgs.gov/x", {"f": "json"})
    finally:
        runtime.stop_log()
    assert len(calls) == 2 and calls[1]["X-Api-Key"] == "SECRET-KEY-123"
    text = log.read_text()
    assert "GET https://api.waterdata.usgs.gov/x?f=json -> 200" in text and "SECRET-KEY-123" not in text


def test_recheck_provisional_flow(tmp_path):
    from validate_runoff import flow_approved, load_cached
    utc = dt.timezone.utc
    key = {"start": "2026-10-21"}
    result = {"run_key": key, "status": "ok", "checked_utc": "2026-10-31T00:00:00+00:00",
              "flow": {"approval_status": {"Provisional": 900}}}
    (tmp_path / "runoff_1.json").write_text(json.dumps(result))
    gpd.GeoDataFrame(geometry=[box(0, 0, 1, 1)], crs=4326).to_file(tmp_path / "runoff_1_basin.geojson")
    soon, late = dt.datetime(2026, 11, 5, tzinfo=utc), dt.datetime(2026, 12, 5, tzinfo=utc)
    assert load_cached(tmp_path, "1", key, now=late) is not None                   # not asked to recheck
    assert load_cached(tmp_path, "1", key, now=soon, recheck_days=30) is not None
    assert load_cached(tmp_path, "1", key, now=late, recheck_days=30) is None      # provisional and old: refetch
    assert flow_approved({"flow": {"approval_status": {"Approved": 10}}})
    assert not flow_approved({"flow": {"approval_status": {"Approved": 10, "Provisional": 2}}})


def test_report_uses_the_ramp_storm_total_used(tmp_path):
    from report import rain_snow_ramp
    out = settings(tmp_path, basin(tmp_path))
    assert rain_snow_ramp(out) == (-1.0, 2.0)                                   # storm.toml
    assert rain_snow_ramp(out, {"rain_snow_ramp_c": [0.5, 2.5]}) == (0.5, 2.5)  # what storm_total.py used
    assert rain_snow_ramp(tmp_path) == (0.0, 3.0)


def test_window_json(tmp_path):
    p = tmp_path / "stations_summary.json"
    p.write_text(json.dumps({"window": {"start": "2026-10-23", "end": "2026-10-23"}}))
    runtime.write_json(runtime.windowed(p, "2026-10-21", "2026-10-22"),
                       {"window": {"start": "2026-10-21", "end": "2026-10-22"}, "n": 1})
    assert runtime.window_json(p, "2026-10-21", "2026-10-22")["n"] == 1   # the window's own copy
    assert runtime.window_json(p, "2026-10-23", "2026-10-23") is not None  # the latest file, when it matches
    assert runtime.window_json(p, "2026-10-01", "2026-10-02") is None


def test_check_endpoints_reports_failures_without_raising(monkeypatch):
    import check_endpoints
    for k, v in OFFLINE.items():
        if "PROXY" in k:
            monkeypatch.setenv(k, v)
    results = check_endpoints.check(["wbd", "usgs"])
    assert [r["ok"] for r in results] == [False, False] and all(r["detail"] for r in results)


def test_gauge_results_are_kept_per_window(tmp_path):
    """Running another storm doesn't throw away a gauge's saved result for the first one."""
    from validate_runoff import load_cached, save_result
    gpd.GeoDataFrame(geometry=[box(0, 0, 1, 1)], crs=4326).to_file(tmp_path / "runoff_1_basin.geojson")
    k1 = {"start": "2026-10-20", "end": "2026-10-27", "after_days": 3}
    k2 = {"start": "2026-11-01", "end": "2026-11-03", "after_days": 7}
    save_result(tmp_path, "1", {"run_key": k1, "status": "ok", "n": 1}, "2026-10-20", "2026-10-27")
    save_result(tmp_path, "1", {"run_key": k2, "status": "ok", "n": 2}, "2026-11-01", "2026-11-03")
    assert load_cached(tmp_path, "1", k1)[0]["n"] == 1 and load_cached(tmp_path, "1", k2)[0]["n"] == 2
    assert json.loads((tmp_path / "runoff_1.json").read_text())["n"] == 2  # the plain name is the latest
    (tmp_path / "runoff_1_20261020_20261027.json").unlink()
    (tmp_path / "runoff_1_20261101_20261103.json").unlink()                  # as saved before per-window files
    assert load_cached(tmp_path, "1", k1) is None and load_cached(tmp_path, "1", k2)[0]["n"] == 2
    assert json.loads((tmp_path / "runoff_1_20261101_20261103.json").read_text())["n"] == 2  # copy kept from now on


def test_report_gauge_names():
    from report import gauge_name
    assert gauge_name("SAN JUAN RIVER AT PAGOSA SPRINGS, CO") == "San Juan River at Pagosa Springs, CO"
    assert gauge_name("ANIMAS RIVER BLW DURANGO PUMP PLANT NR DURANGO, CO") == \
        "Animas River below Durango Pump Plant near Durango, CO"
    assert gauge_name("MCELMO CREEK NEAR BLUFF, UTAH") == "McElmo Creek near Bluff, Utah"
    assert gauge_name("LA PLATA RIVER AT HESPERUS") == "La Plata River at Hesperus"
    assert gauge_name("PIEDRA RIVER NEAR ARBOLES, CO.") == "Piedra River near Arboles, CO"
