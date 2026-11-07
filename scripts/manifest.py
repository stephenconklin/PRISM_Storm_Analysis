"""
Run manifest: how a folder's outputs were made.

<out>/run/run_manifest.json holds {"runs": [...]}, one entry appended per run of any script
that writes to the folder, so a folder refreshed over months keeps its whole history.
Each entry records the command and arguments, start and end time, status (ok, qc_fail,
input_error, incomplete, network_error, error, interrupted; see runtime.py), the log file,
git commit, Python and package versions, the inputs (basin source, per-day PRISM URL, zip
SHA-256, release and info.txt fields), output files with checksums, and the QC results.
Entries with a window also record the PRISM releases of those days in the folder's cubes
(input_releases), so pipeline.py status can tell which outputs predate a revision.

The file is rewritten through a temporary file and a rename, so an interrupted write
never loses the history; an unreadable manifest is set aside, never written over.
"""
import datetime as dt
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

import layout

REPO = Path(__file__).resolve().parents[1]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def info_fields(text):
    """All 'KEY: value' fields of a PRISM info.txt."""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    return fields


def git_commit():
    """Commit of this repository, with '-dirty' if there are uncommitted changes; None outside git."""
    try:
        commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                                text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, check=True).stdout.strip()
        return commit + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return None


def versions():
    """Python and key package versions, including the GDAL and PROJ that rasterio and pyproj use."""
    out = {"python": platform.python_version(), "platform": platform.platform()}
    for name in ("numpy", "rasterio", "xarray", "netCDF4", "geopandas", "shapely", "pyproj", "requests"):
        try:
            module = __import__(name)
            out[name] = getattr(module, "__version__", "unknown")
        except ImportError:
            pass
    try:
        import rasterio
        out["gdal"] = rasterio.__gdal_version__
    except (ImportError, AttributeError):
        pass
    try:
        import pyproj
        out["proj"] = pyproj.proj_version_str
    except (ImportError, AttributeError):
        pass
    return out


HASH_LIMIT_BYTES = 256 * 2 ** 20  # larger outputs (HUC2-size cubes) get size and time instead of a checksum


def files_entry(paths, out):
    """[{path relative to out, bytes, sha256 or modified time}] for the files that exist."""
    rows = []
    for p in paths:
        p = Path(p)
        if p.exists():
            try:
                rel = str(p.resolve().relative_to(Path(out).resolve()))
            except ValueError:
                rel = str(p)
            size = p.stat().st_size
            entry = {"path": rel, "bytes": size}
            if size <= HASH_LIMIT_BYTES:
                entry["sha256"] = sha256(p)
            else:
                entry["modified"] = dt.datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds")
            rows.append(entry)
    return rows


def start_run(script, args):
    """Begin a manifest entry for a run."""
    return {"script": script, "command": [Path(sys.executable).name] + sys.argv, "args": vars(args),
            "started": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "git_commit": git_commit(), "versions": versions()}


def window_releases(out, start, end, res=None):
    """{"<var>_<res>": [release per day]} for start..end in the folder's ppt and tmean cubes (0 = unknown)."""
    try:
        import xarray as xr
    except ImportError:
        return {}
    found = {}
    for nc in sorted(layout.Layout(out, settings={}).daily_dir.glob("*_daily_clip.nc")):
        var, nc_res = nc.name.split("_")[:2]
        if var not in ("ppt", "tmean") or (res and nc_res != res):
            continue
        try:
            with xr.open_dataset(nc) as ds:
                if "release" in ds:
                    found[f"{var}_{nc_res}"] = [int(v) for v in ds.release.sel(time=slice(str(start), str(end))).values]
        except (OSError, ValueError, KeyError):
            continue
    return found


def manifest_path(out):
    return layout.Layout(out, settings={}).manifest_file


def read_runs(out):
    """The runs list of <out>/run/run_manifest.json ([] if there is none or it can't be read)."""
    path = manifest_path(out)
    try:
        return json.loads(path.read_text()).get("runs", []) if path.exists() else []
    except ValueError:
        return []


def append_run(out, entry):
    """Finish the entry and append it to <out>/run/run_manifest.json. Returns the manifest path."""
    entry["finished"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    if "status" not in entry:
        entry["status"] = "qc_fail" if any(c.get("level") == "fail" for c in entry.get("qc") or []) else "ok"
    window = entry.get("window") or {}
    if window.get("start") and window.get("end") and "input_releases" not in entry:
        entry["input_releases"] = window_releases(out, window["start"], window["end"],
                                                  (entry.get("args") or {}).get("res"))
    entry.pop("_appended", None)
    path = manifest_path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    runs = read_runs(out)
    if not runs and path.exists() and path.stat().st_size:
        try:
            json.loads(path.read_text())
        except ValueError:  # unreadable: keep it for inspection instead of writing over it
            path.replace(path.with_name(f"run_manifest.corrupt-{dt.datetime.now():%Y%m%d-%H%M%S}.json"))
    runs.append(entry)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps({"runs": runs}, indent=2, default=str))
    tmp.replace(path)
    entry["_appended"] = True
    return path
