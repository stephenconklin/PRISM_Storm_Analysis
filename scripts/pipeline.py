#!/usr/bin/env python3
"""
Run the PRISM Storm Analysis workflow for one output folder, from its settings (<out>/storm.toml).

    pipeline.py init   set up a folder: find the basin, enter the storm dates, write storm.toml
    pipeline.py run    run every step in order (resumes: steps already done with the same inputs are skipped)
    pipeline.py status what is current, what PRISM has revised, what is still pending, and what to run next
    pipeline.py update pick up PRISM revisions and later flow data, rerun the analysis, and show what changed
    pipeline.py migrate move a folder made before 7 Nov 2026 to the current layout (offline; renames only)

Steps, in order (each runs the script as its own process, as you would by hand):

    ppt        prism_watershed.py               download window                          stops the run if it fails
    tmean      prism_watershed.py --var tmean   download window (rain/snow split)
    stations   validate_stations.py             each event   network (RCC-ACIS)
    totals     storm_total.py                   each event   depth, volume, rain and snow rasters   stops the run
    subbasins  subbasin_totals.py               each event   network (WBD)
    normals    storm_context.py --pct-tif       each event   network (PRISM normals)
    runoff     validate_runoff.py               each event   network (USGS, NLDI, NID); rate-limited runs resume
    snow       validate_snow.py                 each event   network (NRCS AWDB)
    report     report.py                        each event
    qgis       build_qgis_project.py            once, when QGIS is installed

stations runs before totals so that the volume's uncertainty includes the gauge comparison. A failed
network step is recorded and the run goes on; the summary says what to rerun. Every step's output is
also in <out>/run/logs/<time>/, and run/pipeline_state.json records what ran with which inputs.

Each event's outputs go to <out>/events/<event>/ (folder layout: layout.py). Rasters from totals and
normals use standard names the QGIS builder recognizes, in the event's totals/:
    ppt_<res>_total_<start>_<end>.tif, ppt_<res>_volume_..., ppt_<res>_rain_..., ppt_<res>_snow_...,
    ppt_<res>_pct_normal_<start>_<end>.tif

Exit status: 0 everything ran; 3 finished, but some steps failed or are incomplete (rerun later);
another code when a step that stops the run failed (see runtime.py); 130 interrupted.

Usage examples:
    python pipeline.py init                                   # asks for the watershed and dates
    python pipeline.py init --find animas --start 2026-10-21 --end 2026-10-27 --run
    python pipeline.py run --out data/huc1408 --dry-run
    python pipeline.py run --out data/huc1408
    python pipeline.py run --out data/huc1408 --from runoff
    python pipeline.py status --out data/huc1408
    python pipeline.py update --out data/huc1408
    python pipeline.py update --out data/huc1408 --check --notify   # for a scheduled job
    python pipeline.py migrate --out data/huc1408 --dry-run
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import config
import layout
import manifest
import runtime
from layout import Layout
from runtime import EXIT_INCOMPLETE, EXIT_INTERRUPTED, EXIT_OK, die

SCRIPTS = Path(__file__).resolve().parent
FINAL_AFTER_DAYS = 183  # PRISM revises a day for about 6 months (release 8 is final)


# ---------- steps ----------

def span(ev):
    return f"{ev[1].replace('-', '')}_{ev[2].replace('-', '')}"


@dataclass
class Step:
    name: str
    script: str
    per_event: bool
    hard: bool      # a failure stops the run (unless --keep-going)
    network: bool
    args: object    # (ctx, event) -> list of arguments, or a string saying why the step doesn't apply


def download_args(var):
    def f(c, ev):
        if var not in c.vars:
            return f"{var} is not in [download] vars"
        return ["--var", var] + (["--refresh"] if c.refresh else [])
    return f


def window(extra=None):
    def f(c, ev):
        out = ["--start", ev[1], "--end", ev[2]]
        more = extra(c, ev) if extra else []
        return more if isinstance(more, str) else out + more
    return f


def totals_extra(c, ev):
    name = lambda kind: str(c.lay.window_tif(kind, c.res, ev[1], ev[2]))  # noqa: E731
    x = ["--tif", name("total"), "--volume-tif", name("volume")]
    if c.lay.cube("tmean", c.res).exists():
        x += ["--rain-tif", name("rain"), "--snow-tif", name("snow")]
    return x


def qgis_args(c, ev):
    if c.no_qgis:
        return "--no-qgis"
    from build_qgis_project import find_qgis_python
    try:
        import qgis.core  # noqa: F401
        found = True
    except ImportError:
        found = bool(find_qgis_python(c.qgis_python)[0])
    if not found:
        return "QGIS not found (pass --qgis-python or set QGIS_PYTHON)"
    return ["--qgis-python", c.qgis_python] if c.qgis_python else []


STEPS = [
    Step("ppt", "prism_watershed.py", False, True, True, download_args("ppt")),
    Step("tmean", "prism_watershed.py", False, False, True, download_args("tmean")),
    Step("stations", "validate_stations.py", True, False, True, window()),
    Step("totals", "storm_total.py", True, True, False, window(totals_extra)),
    Step("subbasins", "subbasin_totals.py", True, False, True, window()),
    Step("normals", "storm_context.py", True, False, True,
         window(lambda c, ev: ["--pct-tif", str(c.lay.window_tif("pct_normal", c.res, ev[1], ev[2]))])),
    Step("runoff", "validate_runoff.py", True, False, True,
         window(lambda c, ev: ["--recheck-provisional-days", "30"] if c.refresh else [])),
    Step("snow", "validate_snow.py", True, False, True, window()),
    Step("report", "report.py", True, False, False,
         window(lambda c, ev: ["--keep-previous"] if c.refresh else [])),
    Step("qgis", "build_qgis_project.py", False, False, False, qgis_args),
]
STEP_NAMES = [s.name for s in STEPS]
ALWAYS_AFTER_CHANGES = {"report", "qgis"}  # rerun whenever an earlier step ran in the same invocation


class Ctx:
    def __init__(self, out, a=None, refresh=False):
        self.out = Path(out)
        if layout.legacy(self.out):
            die(layout.migrate_hint(self.out))
        try:
            self.s = config.load(self.out)
        except config.SettingsError as e:
            die(str(e))
        if self.s is None:
            die(f"No {config.FILE} in {self.out}. Set the folder up first: python scripts/pipeline.py init")
        problems = config.check(self.s)
        if problems:
            die(f"{config.path_for(self.out)}: " + "; ".join(problems))
        self.res = self.s.get("res", "800m")
        self.region = self.s.get("region", "us")
        self.vars = (self.s.get("download") or {}).get("vars", ["ppt", "tmean"])
        self.events = config.events(self.s)
        self.lay = Layout(self.out, self.s)
        self.refresh = refresh
        self.no_qgis = getattr(a, "no_qgis", False)
        self.qgis_python = getattr(a, "qgis_python", None)
        self.yes = getattr(a, "yes", False)
        self.verbose = getattr(a, "verbose", False)
        self.quiet = getattr(a, "quiet", False)


def file_sha(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


UPDATE_ONLY = {"--refresh": 0, "--keep-previous": 0, "--recheck-provisional-days": 1}  # flag: values it takes


def fingerprint(c, step, args):
    """What a step's result depends on: its arguments (without update's --refresh and the like), the settings
    file and, after the downloads, the PRISM releases in the folder (the release history CSVs change only
    when a new release arrives)."""
    kept, skip = [], 0
    for x in args:
        if skip:
            skip -= 1
        elif x in UPDATE_ONLY:
            skip = UPDATE_ONLY[x]
        else:
            kept.append(x)
    h = hashlib.sha256(json.dumps([step.name, kept]).encode())
    h.update(file_sha(config.path_for(c.out)).encode())
    if step.name not in ("ppt", "tmean"):
        for var in ("ppt", "tmean"):
            h.update(file_sha(c.lay.release_history(var, c.res)).encode())
    if step.name == "totals":  # the gauge comparison goes into the volume's uncertainty
        start, end = args[args.index("--start") + 1], args[args.index("--end") + 1]
        h.update(file_sha(c.lay.validation_file("stations_summary.json", start, end)).encode())
    return h.hexdigest()[:16]


def state_path(out):
    return Layout(out, settings={}).state_file


def load_state(out):
    try:
        return json.loads(state_path(out).read_text())
    except (OSError, ValueError):
        return {"steps": {}}


def plan(c, a, state, force_event_steps=False):
    """[(step, event or None, key, args or skip reason, action)] in run order. action is 'run', 'done'
    (finished earlier with the same inputs) or 'skip'."""
    events = c.events
    if getattr(a, "event", None):
        events = [e for e in events if e[0] in a.event]
        if not events:
            die(f"No event named {', '.join(a.event)} in {config.FILE} (events: {', '.join(e[0] for e in c.events)})")
    names = STEP_NAMES
    if getattr(a, "only", None):
        names = [n for n in names if n in a.only]
    if getattr(a, "skip", None):
        names = [n for n in names if n not in a.skip]
    start_at = STEP_NAMES.index(a.from_step) if getattr(a, "from_step", None) else None
    items = []
    for step in STEPS:
        if step.name not in names:
            continue
        for ev in (events if step.per_event else [None]):
            key = step.name + (f":{ev[0]}" if ev else "")
            args = step.args(c, ev)
            if isinstance(args, str):
                items.append((step, ev, key, args, "skip"))
                continue
            forced = (getattr(a, "force", False) or getattr(a, "only", None)
                      or (start_at is not None and STEP_NAMES.index(step.name) >= start_at)
                      or (force_event_steps and step.per_event) or c.refresh and not step.per_event)
            prev = state["steps"].get(key) or {}
            done = prev.get("status") == "ok" and prev.get("fingerprint") == fingerprint(c, step, args)
            items.append((step, ev, key, args, "run" if forced or not done else "done"))
    return items


def command(c, step, args):
    common = [] if step.name == "qgis" else (["--yes"] + (["--quiet"] if c.quiet else [])
                                             + (["--verbose"] if c.verbose else []))
    return [sys.executable, str(SCRIPTS / step.script), "--out", str(c.out), *args, *common]


def label(step, ev):
    return step.name + (f" · {ev[0]} ({ev[1]} to {ev[2]})" if ev else "")


def execute(c, items, a):
    """Run the plan. Returns [(key, step, ev, status, code, seconds, log)]."""
    state = load_state(c.out)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = c.lay.logs_dir / stamp
    env = {**os.environ, runtime.LOCK_ENV: str(c.lay.lock_file.resolve()),
           runtime.PIPELINE_ENV: shlex.join(["python"] + sys.argv)}
    results, ran_any, stopped, k = [], False, None, 0
    todo = [i for i in items if i[4] == "run" or (i[4] == "done" and i[0].name in ALWAYS_AFTER_CHANGES)]
    started = time.monotonic()
    for n, (step, ev, key, args, action) in enumerate(items):
        if stopped:
            results.append((key, step, ev, f"not run ({stopped})", None, 0, ""))
            continue
        if action == "skip":
            results.append((key, step, ev, f"skipped: {args}", None, 0, ""))
            continue
        if action == "done" and not (step.name in ALWAYS_AFTER_CHANGES and ran_any):
            results.append((key, step, ev, "done earlier", None, 0, ""))
            continue
        args = step.args(c, ev)  # again now: earlier steps may have made inputs (the tmean cube for totals)
        if isinstance(args, str):
            results.append((key, step, ev, f"skipped: {args}", None, 0, ""))
            continue
        k += 1
        log = log_dir / f"{n + 1:02d}-{key.replace(':', '-')}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        cmd = command(c, step, args)
        print(f"\n── [{k}/{len(todo)}] {label(step, ev)}{runtime.eta(started, k - 1, len(todo))}", flush=True)
        if c.verbose:
            print("   " + shlex.join(cmd), flush=True)
        t0 = time.monotonic()
        tee = step.name == "qgis"  # the QGIS builder runs under QGIS's Python and keeps no log: copy its output
        proc = subprocess.Popen(cmd, env={**env, runtime.LOG_ENV: str(log)}, text=True,
                                stdout=subprocess.PIPE if tee else None, stderr=subprocess.STDOUT if tee else None)
        try:
            if tee:
                with open(log, "a", encoding="utf-8") as f:
                    for line in proc.stdout:
                        print(line, end="", flush=True)
                        f.write(dt.datetime.now().strftime("%H:%M:%S ") + line)
            code = proc.wait()
        except KeyboardInterrupt:  # Ctrl-C reached the step too: let it record what it finished, then stop
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
            code = EXIT_INTERRUPTED
        seconds = time.monotonic() - t0
        status = runtime.status_name(code)
        ran_any = True
        state["steps"][key] = {"status": status, "code": code, "fingerprint": fingerprint(c, step, args),
                               "finished": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                               "seconds": round(seconds, 1), "log": str(log.relative_to(c.out)),
                               "command": shlex.join(cmd)}
        runtime.write_json(c.lay.state_file, state)
        results.append((key, step, ev, status, code, seconds, str(log.relative_to(c.out))))
        if code == EXIT_INTERRUPTED:
            print_summary(results)
            raise KeyboardInterrupt
        if code and step.hard and not getattr(a, "keep_going", False):
            stopped = f"{step.name} failed"
            print(f"\n{step.name} failed (exit {code}, {status}); later steps need it, so the run stops here. "
                  f"Fix the problem (log: {log.relative_to(c.out)}), then rerun; finished steps are skipped.")
    return results


def fmt_time(seconds):
    return "" if not seconds else f"{seconds:.0f} s" if seconds < 90 else f"{seconds / 60:.1f} min"


def print_summary(results):
    print(f"\n{'Step':<10} {'Event':<14} {'Result':<34} {'Time':>8}  Log")
    for key, step, ev, status, code, seconds, log in results:
        print(f"{step.name:<10} {(ev[0] if ev else '—'):<14} {status[:34]:<34} {fmt_time(seconds):>8}  {log}")


def exit_code(results):
    hard = [r for r in results if r[4] and r[1].hard]
    if hard:
        return hard[0][4]
    if any(r[4] for r in results):
        return EXIT_INCOMPLETE
    return EXIT_OK


def print_plan(c, items):
    print(f"{'Step':<10} {'Event':<14} {'Action':<10} Command")
    for step, ev, key, args, action in items:
        what = shlex.join(command(c, step, args)[1:]) if action != "skip" else f"({args})"
        print(f"{step.name:<10} {(ev[0] if ev else '—'):<14} {action:<10} {what}")


def preflight(c, items, offline=False):
    """Before a run: which services the steps need answer, and is there disk space for the downloads."""
    ok = True
    if not offline:
        import check_endpoints
        names = sorted({s for step, ev, key, args, action in items if action == "run"
                        for s in check_endpoints.STEP_SERVICES.get(step.name, [])})
        if names:
            print("\nServices the planned steps use:")
            results = check_endpoints.check(names)
            print("\n".join(check_endpoints.report(results)))
            ok = all(r["ok"] for r in results)
    need, measured = download_estimate(c)
    probe = c.out if c.out.exists() else next(p for p in c.out.resolve().parents if p.exists())
    free = shutil.disk_usage(probe).free
    print(f"\nDisk: about {need / 1e9:.1f} GB more for downloads and unzipped grids"
          f"{'' if measured else ' (rough: no zips cached yet to measure)'}; {free / 1e9:.0f} GB free.")
    if need > free:
        print("Not enough free disk space for the downloads.")
        ok = False
    return ok


def download_estimate(c):
    """(bytes, measured): space for zips not yet cached plus their unzipped grids."""
    d = c.s["download"]
    start, end = dt.date.fromisoformat(config.iso(d["start"])), dt.date.fromisoformat(config.iso(d["end"]))
    end = min(end, dt.date.today() - dt.timedelta(days=1))
    missing, sizes = 0, []
    for var in c.vars:
        for i in range((end - start).days + 1):
            z = c.lay.store.zip_path(var, c.res, c.region, start + dt.timedelta(days=i))
            if z.exists():
                sizes.append(z.stat().st_size)
            else:
                missing += 1
    each = sorted(sizes)[len(sizes) // 2] if sizes else (15e6 if c.res == "800m" else 1e6)
    return missing * each * 2, bool(sizes)


def open_outputs(c):
    paths = [c.lay.report_html(ev[1], ev[2]) for ev in c.events]
    paths += sorted(c.lay.qgis_dir.glob("*.qgz"))
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    for p in paths:
        if p.exists() and shutil.which(opener):
            subprocess.run([opener, str(p)], check=False)


# ---------- status ----------

def cube_releases(c, var):
    """{date: release} for the days in the folder's cube for var."""
    nc = c.lay.cube(var, c.res)
    if not nc.exists():
        return {}
    import xarray as xr
    with xr.open_dataset(nc) as ds:
        days = ds.time.values.astype("datetime64[D]").astype(object)
        rel = ds.release.values if "release" in ds else [0] * len(days)
    return {d: int(r) for d, r in zip(days, rel)}


def latest_run(runs, script, ev, finished_only=True):
    """The newest manifest entry of script for the event's window (with finished_only, one that wrote its
    outputs: status ok or qc_fail)."""
    for run in reversed(runs):
        w = run.get("window") or {}
        if run.get("script") == script and w.get("start") == ev[1] and w.get("end") == ev[2] \
                and (not finished_only or run.get("status", "ok") in ("ok", "qc_fail")):
            return run
    return None


def attempts(runs, script, ev):
    """Status of the newest run of script for the event, matched by the --start/--end it was given."""
    for run in reversed(runs):
        args = run.get("args") or {}
        if run.get("script") == script and args.get("start") == ev[1] and args.get("end") == ev[2]:
            return run.get("status", "ok")
    return None


EVENT_OUTPUTS = [("stations", "validate_stations.py"), ("totals", "storm_total.py"), ("subbasins", "subbasin_totals.py"),
                 ("normals", "storm_context.py"), ("runoff", "validate_runoff.py"), ("snow", "validate_snow.py"),
                 ("report", "report.py")]


def folder_status(c, offline=False):
    """What the folder holds and what to do next, as a dict (printed by print_status)."""
    d = c.s["download"]
    start, end = dt.date.fromisoformat(config.iso(d["start"])), dt.date.fromisoformat(config.iso(d["end"]))
    yesterday = dt.date.today() - dt.timedelta(days=1)
    wanted = [start + dt.timedelta(days=i) for i in range((min(end, yesterday) - start).days + 1)]
    st = {"vars": {}, "events": [], "actions": [], "final_by": (end + dt.timedelta(days=FINAL_AFTER_DAYS)).isoformat()}
    session = None
    if not offline:
        import requests
        session = requests.Session()
        session.headers["User-Agent"] = "prism-storm-analysis"
    from prism_watershed import available_releases
    for var in c.vars:
        have = cube_releases(c, var)
        missing = [x for x in wanted if x not in have]
        avail = available_releases(var, c.res, c.region, start, min(end, yesterday), session) if session else None
        newer = [(x, have[x], avail[x]) for x in sorted(have) if avail and avail.get(x, 0) > have[x] and x in wanted]
        counts = {}
        for x in wanted:
            if x in have:
                counts[have[x]] = counts.get(have[x], 0) + 1
        st["vars"][var] = {"days": len(wanted), "missing": [str(x) for x in missing], "releases": counts,
                           "newer": [(str(x), h, n) for x, h, n in newer], "checked": avail is not None}
        if missing:
            st["actions"].append(f"{len(missing)} {var} day(s) not downloaded yet: pipeline.py run --out {c.out}")
        if newer:
            st["actions"].append(f"PRISM has revised {len(newer)} {var} day(s): pipeline.py update --out {c.out}")
    runs = manifest.read_runs(c.out)
    current = cube_releases(c, "ppt")
    for ev in c.events:
        e = {"name": ev[0], "start": ev[1], "end": ev[2], "outputs": {}, "stale": [], "flow_pending": 0,
             "provisional": 0}
        days = [dt.date.fromisoformat(ev[1]) + dt.timedelta(days=i)
                for i in range((dt.date.fromisoformat(ev[2]) - dt.date.fromisoformat(ev[1])).days + 1)]
        now_rel = [current.get(x, 0) for x in days]
        for name, script in EVENT_OUTPUTS:
            run = latest_run(runs, script, ev)
            last = attempts(runs, script, ev)
            e["outputs"][name] = (run.get("status", "ok") if run and last in (None, "ok", "qc_fail")
                                  else f"failed ({last})" if last else "missing")
            made_from = ((run or {}).get("input_releases") or {}).get(f"ppt_{c.res}")
            if run and made_from is not None and made_from != now_rel:
                e["stale"].append(name)
        summary = c.lay.validation_file("runoff_summary.csv", ev[1], ev[2])
        if summary.exists():
            import csv
            from validate_runoff import flow_approved, result_paths
            with open(summary, newline="") as f:
                rows = [r for r in csv.DictReader(f) if r.get("status") == "ok"]
            e["flow_pending"] = sum(r.get("flow_complete") == "False" for r in rows)
            for r in rows:
                try:
                    saved = next(p for p in result_paths(c.lay.runoff_dir(ev[1], ev[2]), r["site"], ev[1], ev[2])
                                 if p.exists())
                    if not flow_approved(json.loads(saved.read_text())):
                        e["provisional"] += 1
                except (OSError, ValueError, StopIteration):
                    pass
        if any(v == "missing" or v.startswith("failed") for v in e["outputs"].values()):
            st["actions"].append(f"event {ev[0]}: some outputs missing or failed last time: "
                                 f"pipeline.py run --out {c.out}")
        if e["stale"]:
            st["actions"].append(f"event {ev[0]}: {', '.join(e['stale'])} made from older PRISM releases: "
                                 f"pipeline.py update --out {c.out}")
        if e["flow_pending"]:
            days_after = ((config.event_for_window(c.s, ev[1], ev[2]) or {}).get("after_days")
                          or (c.s.get("validation") or {}).get("after_days", 7))
            after = dt.date.fromisoformat(ev[2]) + dt.timedelta(days=int(days_after))
            st["actions"].append(f"event {ev[0]}: {e['flow_pending']} gauge(s) recorded before the flow window "
                                 f"ended ({after}); after that: pipeline.py update --out {c.out}")
        st["events"].append(e)
    qgz = sorted(c.lay.qgis_dir.glob("*.qgz"))
    st["qgis"] = str(qgz[0].relative_to(c.out)) if qgz else None
    return st


def print_status(c, st):
    print(f"{c.s.get('name') or c.out.name}: {c.s.get('huc') and 'HUC ' + str(c.s['huc']) or c.s.get('boundary')}, "
          f"{c.res}; settings {config.path_for(c.out)}")
    for var, v in st["vars"].items():
        rel = ", ".join(f"r{r}: {n}" for r, n in sorted(v["releases"].items())) or "none"
        line = f"  {var:<6} {v['days'] - len(v['missing'])}/{v['days']} days ({rel})"
        if not v["checked"]:
            line += "; PRISM's release service not checked"
        elif v["newer"]:
            line += f"; newer at PRISM for {len(v['newer'])}: " + ", ".join(f"{x[5:]} r{h}->r{n}" for x, h, n in v["newer"][:6])
        else:
            line += "; up to date with PRISM"
        print(line)
    print(f"  PRISM revises days for about 6 months; final releases (r8) expected by about {st['final_by']}.")
    for e in st["events"]:
        outs = "  ".join(f"{k} {'✓' if v == 'ok' else v}" for k, v in e["outputs"].items())
        print(f"  event {e['name']} ({e['start']} to {e['end']}): {outs}")
        if e["stale"]:
            print(f"    made from older PRISM releases: {', '.join(e['stale'])}")
        if e["flow_pending"] or e["provisional"]:
            print(f"    gauges: {e['flow_pending']} with the flow window not over when fetched, "
                  f"{e['provisional']} with provisional USGS flow (USGS approves it over months)")
    print(f"  QGIS project: {st['qgis'] or 'not built'}")
    if st["actions"]:
        print("Next:")
        for x in dict.fromkeys(st["actions"]):
            print(f"  - {x}")
    else:
        print(f"Nothing to do now; check again later (pipeline.py update --out {c.out} --check).")


# ---------- update: what changed ----------

def metrics(c, ev):
    m = {}
    vfile = lambda name: c.lay.validation_file(name, ev[1], ev[2])  # noqa: E731
    unc = c.lay.uncertainty_json(c.res, ev[1], ev[2])
    if unc.exists():
        u = json.loads(unc.read_text())
        m.update(volume=u["volume_m3"], range=u.get("range_m3"), releases=u.get("releases"))
    sj = runtime.window_json(vfile("stations_summary.json"), ev[1], ev[2])
    if sj:
        m["gauge_pbias"] = ((sj.get("event_totals") or {}).get("all") or {}).get("pbias_pct")
    summary = vfile("runoff_summary.csv")
    if summary.exists():
        import csv
        with open(summary, newline="") as f:
            m["runoff"] = {r["site"]: (r.get("ratio_low"), r.get("ratio_high"), r.get("name", ""))
                           for r in csv.DictReader(f) if r.get("status") == "ok"}
    sn = runtime.window_json(vfile("snotel_summary.json"), ev[1], ev[2])
    if sn:
        m["snow_ratio"] = sn.get("prism_over_observed_snow")
    return m


def print_changes(ev, before, after):
    print(f"\nEvent {ev[0]} ({ev[1]} to {ev[2]}):")
    if before.get("volume") and after.get("volume"):
        v0, v1 = before["volume"], after["volume"]
        print(f"  volume {v0 / 1e9:.3f} -> {v1 / 1e9:.3f} km³ ({(v1 - v0) / v0 * 100:+.2f}%)"
              + (f"; range {after['range'][0] / 1e9:.3f}-{after['range'][1] / 1e9:.3f} km³" if after.get("range") else ""))
    elif after.get("volume"):
        print(f"  volume {after['volume'] / 1e9:.3f} km³ (first total for this event)")
    if before.get("releases") != after.get("releases") and after.get("releases"):
        print(f"  PRISM releases {sorted(set(before.get('releases') or []))} -> {sorted(set(after['releases']))}")
    if before.get("gauge_pbias") != after.get("gauge_pbias") and after.get("gauge_pbias") is not None:
        print(f"  PRISM vs rain gauges {before.get('gauge_pbias')}% -> {after['gauge_pbias']}%")
    changed = [(s, before.get("runoff", {}).get(s), r) for s, r in (after.get("runoff") or {}).items()
               if before.get("runoff", {}).get(s) != r]
    for site, old, new in changed[:10]:
        print(f"  runoff ratio {site} {new[2][:30]}: {f'{old[0]}–{old[1]}' if old else 'new'} -> {new[0]}–{new[1]}")
    if len(changed) > 10:
        print(f"  … and {len(changed) - 10} more gauges")
    if before.get("snow_ratio") != after.get("snow_ratio") and after.get("snow_ratio") is not None:
        print(f"  PRISM snow / SNOTEL SWE gain {before.get('snow_ratio')} -> {after['snow_ratio']}")
    if before == after:
        print("  no change")


def notify(message):
    """A desktop notification on macOS; printed elsewhere."""
    print(message)
    if sys.platform == "darwin" and shutil.which("osascript"):
        text = message.replace('"', "'")[:240]
        subprocess.run(["osascript", "-e", f'display notification "{text}" with title "PRISM Storm Analysis"'],
                       check=False)


# ---------- commands ----------

def cmd_run(a, refresh=False, force_event_steps=False):
    c = Ctx(a.out, a, refresh=refresh)
    items = plan(c, a, load_state(c.out), force_event_steps)
    if a.dry_run:
        print_plan(c, items)
        ok = preflight(c, items, a.offline)
        return EXIT_OK if ok else runtime.EXIT_NETWORK
    run = manifest.start_run("pipeline.py", a)
    runtime.attach(c.out, run, a, log_path=c.lay.logs_dir / f"{dt.datetime.now():%Y%m%d-%H%M%S}-pipeline.log")
    # Settle each event's folder first (an event renamed or redated in storm.toml), so the output paths the
    # steps are given point into the folder they will keep.
    for ev in c.events:
        c.lay.prepare_event(ev[1], ev[2])
    items = plan(c, a, load_state(c.out), force_event_steps)
    todo = sum(i[4] == "run" for i in items)
    print(f"{c.s.get('name') or c.out.name}: {todo} step(s) to run, "
          f"{sum(i[4] == 'done' for i in items)} done earlier with the same inputs"
          + (" (--force reruns them)" if any(i[4] == "done" for i in items) else ""))
    results = execute(c, items, a)
    print_summary(results)
    code = exit_code(results)
    if code == EXIT_INCOMPLETE:
        print("\nSome steps failed or are incomplete (see the logs above). Rerun the same command later: "
              "finished steps are skipped and rate-limited gauges pick up where they stopped.")
    run.update(steps=[{"key": k, "status": s, "code": cd, "seconds": round(t, 1), "log": lg}
                      for k, _, _, s, cd, t, lg in results])
    if code:
        run["status"] = runtime.status_name(code)
    manifest.append_run(c.out, run)
    if getattr(a, "open", False):
        open_outputs(c)
    return code


def cmd_status(a):
    c = Ctx(a.out, a)
    print_status(c, folder_status(c, a.offline))
    return EXIT_OK


def cmd_update(a):
    c = Ctx(a.out, a)
    st = folder_status(c, a.offline)
    print_status(c, st)
    if a.check:
        if a.notify and st["actions"]:
            notify(f"{c.s.get('name') or c.out.name}: " + "; ".join(dict.fromkeys(x.split(':')[0] for x in st["actions"])))
        return EXIT_OK
    newer = sum(len(v["newer"]) for v in st["vars"].values())
    runtime.confirm_or_stop(
        f"\nUpdate {c.out}: re-download {newer if st['vars'] and all(v['checked'] for v in st['vars'].values()) else 'any'} "
        "revised PRISM file(s), fetch later and revised flow, and rerun every event's analysis?", a.yes)
    before = {ev: metrics(c, ev) for ev in c.events}
    a.force, a.only, a.skip, a.from_step = False, None, getattr(a, "skip", None), None
    code = cmd_run(a, refresh=True, force_event_steps=True)
    for ev in c.events:
        print_changes(ev, before[ev], metrics(c, ev))
    return code


def cmd_migrate(a):
    """Move a folder in the old layout (everything at the top) to the current one (layout.py)."""
    import migrate
    out = Path(a.out)
    if not out.is_dir():
        die(f"No folder {out}")
    if not layout.legacy(out):
        print(f"{out} already uses the current folder layout; nothing to move.")
        return EXIT_OK
    plan = migrate.Plan(out, a.store).build()
    plan.print(a.verbose)
    if a.dry_run:
        print("\nDry run: nothing was changed. Run again without --dry-run to move the files.")
        return EXIT_OK
    runtime.confirm_or_stop(f"\nMove the files in {out} as listed (renames; nothing is downloaded)?", a.yes)
    lock = runtime.FolderLock(out).acquire()
    try:
        run = manifest.start_run("pipeline.py", a)
        done = plan.execute()
        kept = migrate.carry_over_state(out)
        run.update(migration={"layout": layout.VERSION, "store": str(plan.lay.store.root), **done,
                              "pipeline_steps_kept": kept, "not_recognized": [str(p) for p in plan.unknown]})
        run["status"] = "ok"
        manifest.append_run(out, run)
    finally:
        lock.release()
    print(f"\nMoved {done['moved']}, removed {done['deleted']} duplicates or leftovers, "
          f"{done['store_deduplicated']} already in the store, {done['set_aside']} set aside in run/pre_migration/, "
          f"{done['layers']} GeoPackage layers copied per window; {kept} finished pipeline steps stay finished.")
    if layout.legacy(out):
        print("Some old-layout items remain (listed above as not recognized); move or delete them, then rerun.")
    if not a.no_qgis and plan.lay.qgis_dir.exists():
        print(migrate.rebuild_qgis(out, a.qgis_python))
    return EXIT_OK


def ask(prompt, default=None, check=None):
    while True:
        try:
            ans = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
        except EOFError:
            die("No answer; pass the values as options instead (see --help).")
        ans = ans or (default or "")
        if not ans:
            continue
        try:
            return check(ans) if check else ans
        except ValueError as e:
            print(f"  {e}")


def iso_date(text):
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError("use YYYY-MM-DD") from None


def choose_basin(a):
    """(huc, boundary, name) from --huc, --boundary or a WBD name search."""
    if a.huc or a.boundary:
        return a.huc, str(Path(a.boundary).resolve()) if a.boundary else None, a.name
    name = a.find or (ask("Watershed name to search for (part of a WBD name)") if runtime.interactive() else None)
    if not name:
        die("Pass --huc CODE, --boundary FILE or --find NAME.")
    import find_huc
    rows = [r for d in a.digits for r in find_huc.search(name, d, a.state, 30)]
    if not rows:
        die(f"No WBD units named like '{name}' at levels {a.digits}.")
    if len(rows) == 1 and not runtime.interactive():
        return rows[0][0], None, rows[0][2]
    if not runtime.interactive():
        die("Several watersheds match; pass --huc with one of:\n" + "\n".join(
            f"  {h:<12} {find_huc.LEVELS[d]:<12} {area:>10,.0f} km²  {st:<10} {n}" for h, d, n, st, area in rows))
    for i, (h, d, n, st, area) in enumerate(rows, 1):
        print(f"  {i:>2}. {h:<12} {find_huc.LEVELS[d]:<12} {area:>10,.0f} km²  {st:<10} {n}")

    def pick(x):
        if not x.isdigit() or not 1 <= int(x) <= len(rows):
            raise ValueError(f"enter 1 to {len(rows)}")
        return rows[int(x) - 1]
    h, d, n, st, area = ask("Number of the watershed to use", check=pick)
    return h, None, n


def cmd_init(a):
    huc, boundary, basin_name = choose_basin(a)
    if huc and not (huc.isdigit() and len(huc) % 2 == 0 and 2 <= len(huc) <= 16):
        die("--huc must be 2, 4, 6, 8, 10, 12, 14 or 16 digits")
    tty = runtime.interactive()
    start = iso_date(a.start) if a.start else (ask("First PRISM day of the storm (YYYY-MM-DD)", check=iso_date) if tty else None)
    end = iso_date(a.end) if a.end else (ask("Last PRISM day of the storm", str(start), check=iso_date) if tty else None)
    if not (start and end):
        die("Pass --start and --end (the storm's PRISM days).")
    if end < start:
        die("--end is before --start.")
    event = a.event or (ask("Name for this event", "storm") if tty else "storm")
    yesterday = dt.date.today() - dt.timedelta(days=1)
    d0, d1 = start - dt.timedelta(days=a.pad), min(end + dt.timedelta(days=a.pad), yesterday)
    out = Path(a.out or (f"data/huc{huc}" if huc else f"data/{Path(boundary).stem}"))
    s = {"name": a.name or basin_name or out.name, "huc": huc, "boundary": boundary, "res": a.res,
         "prism_store": str(Path(a.store).expanduser().resolve()) if a.store else None, "download": {"start": d0, "end": d1, "vars": a.vars},
         "events": [{"name": event, "start": start, "end": end}],
         "rain_snow": {"all_snow_c": 0.0, "all_rain_c": 3.0},
         "validation": {"buffer_km": 10.0, "after_days": 7, "gauges": []}}
    path = config.path_for(out)
    if path.exists() and not a.force:
        if not (tty and runtime.confirm(f"{path} exists. Replace it?")):
            die(f"{path} exists; pass --force to replace it, or edit it by hand.")
    out.mkdir(parents=True, exist_ok=True)
    runtime.write_atomic(path, config.render(s))
    problems = config.check(config.load(out))
    if problems:
        die(f"{path}: " + "; ".join(problems))
    print(f"Wrote {path}:\n")
    print(path.read_text())
    if end + dt.timedelta(days=a.pad) > yesterday:
        print(f"Note: the download ends {d1} (yesterday); extend [download] end later and rerun to add days.")
    a.out, a.event, a.only, a.skip, a.from_step, a.force = str(out), None, None, None, None, False
    if a.run or (tty and runtime.confirm("Run the pipeline now?", default=True)):
        a.dry_run = False
        return cmd_run(a)
    c = Ctx(out, a)
    print("Plan (run it with: python scripts/pipeline.py run --out " + shlex.quote(str(out)) + "):")
    items = plan(c, a, load_state(out))
    print_plan(c, items)
    preflight(c, items, a.offline)
    return EXIT_OK


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p, out_required=True):
        p.add_argument("--out", required=out_required, help="the output folder (holds storm.toml)")
        runtime.add_common_args(p)

    def run_opts(p):
        p.add_argument("--event", nargs="+", help="only these events (default: every event in storm.toml)")
        p.add_argument("--skip", nargs="+", choices=STEP_NAMES, help="leave these steps out")
        p.add_argument("--keep-going", action="store_true", help="go on after a step that normally stops the run")
        p.add_argument("--offline", action="store_true", help="with --dry-run: don't check the services")
        p.add_argument("--open", action="store_true", help="open the reports and the QGIS project when done")
        p.add_argument("--no-qgis", action="store_true", help="don't build the QGIS project")
        p.add_argument("--qgis-python", help="QGIS's Python for the qgis step (else found automatically)")

    p = sub.add_parser("init", help="set up a folder's storm.toml (asks for what isn't given)")
    common(p, out_required=False)
    p.add_argument("--find", help="search WBD names for the basin (instead of --huc/--boundary)")
    p.add_argument("--huc", help="WBD hydrologic unit code")
    p.add_argument("--boundary", help="your own basin polygon file")
    p.add_argument("--digits", type=int, nargs="+", default=[4, 6, 8, 10], help="HUC levels for --find (default 4 6 8 10)")
    p.add_argument("--state", help="two-letter state the basin must touch (for --find)")
    p.add_argument("--start", help="first PRISM day of the storm (YYYY-MM-DD)")
    p.add_argument("--end", help="last PRISM day of the storm")
    p.add_argument("--event", help="name for the event (default storm)")
    p.add_argument("--name", help="a name for the folder's basin and storm (default: the WBD name)")
    p.add_argument("--pad", type=int, default=1, help="days to download either side of the storm (default 1)")
    p.add_argument("--res", default="800m", choices=["800m", "4km"])
    p.add_argument("--vars", nargs="+", default=["ppt", "tmean"], help="PRISM variables to download (default ppt tmean)")
    p.add_argument("--store", "--cache", dest="store",
                   help="the PRISM store shared by every basin (default: prism_store/ next to the folder)")
    p.add_argument("--force", action="store_true", help="replace an existing storm.toml")
    p.add_argument("--run", action="store_true", help="run the pipeline right after writing the settings")
    p.add_argument("--no-qgis", action="store_true", help="(with --run) don't build the QGIS project")
    p.add_argument("--qgis-python", help="(with --run) QGIS's Python")
    p.add_argument("--offline", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_init, open=False, keep_going=False)

    p = sub.add_parser("run", help="run the steps (resuming: steps done with the same inputs are skipped)")
    common(p)
    run_opts(p)
    p.add_argument("--from", dest="from_step", choices=STEP_NAMES, help="rerun from this step on")
    p.add_argument("--only", nargs="+", choices=STEP_NAMES, help="run only these steps")
    p.add_argument("--force", action="store_true", help="rerun steps even if they are done with the same inputs")
    p.add_argument("--dry-run", action="store_true",
                   help="show the plan and commands, check the services and disk space, run nothing")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="show what is current, revised or pending (no downloads)")
    common(p)
    p.add_argument("--offline", action="store_true", help="don't ask PRISM which releases it has")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("update", help="pick up PRISM revisions and later flow; rerun and show what changed")
    common(p)
    run_opts(p)
    p.add_argument("--check", action="store_true", help="only show what would be updated (for scheduled jobs)")
    p.add_argument("--notify", action="store_true", help="with --check: a desktop notification when there is something to do")
    p.set_defaults(func=cmd_update, dry_run=False)

    p = sub.add_parser("migrate", help="move a folder made before 7 Nov 2026 to the current folder layout (offline)")
    common(p)
    p.add_argument("--dry-run", action="store_true", help="list the moves; change nothing")
    p.add_argument("--store", help="the PRISM store to move the national data into (default: prism_store in "
                                   "storm.toml, else PRISM_STORE, else prism_store/ next to the folder)")
    p.add_argument("--no-qgis", action="store_true", help="don't rebuild the QGIS project afterwards")
    p.add_argument("--qgis-python", help="QGIS's Python for the rebuild (else found automatically)")
    p.set_defaults(func=cmd_migrate)

    a = ap.parse_args()
    sys.exit(a.func(a))


if __name__ == "__main__":
    runtime.run_main(main)
