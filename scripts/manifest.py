"""
Run manifest: how a folder's outputs were made.

<out>/run_manifest.json holds {"runs": [...]}, one entry appended per run of
prism_watershed.py or storm_total.py, so a folder refreshed over months keeps its
whole history. Each entry records the command and arguments, start and end time,
git commit, Python and package versions, the inputs (basin source, per-day PRISM
URL, zip SHA-256, release and info.txt fields), output files with checksums, and
the QC results.
"""
import datetime as dt
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

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


def files_entry(paths, out):
    """[{path relative to out, bytes, sha256}] for the files that exist."""
    rows = []
    for p in paths:
        p = Path(p)
        if p.exists():
            try:
                rel = str(p.resolve().relative_to(Path(out).resolve()))
            except ValueError:
                rel = str(p)
            rows.append({"path": rel, "bytes": p.stat().st_size, "sha256": sha256(p)})
    return rows


def start_run(script, args):
    """Begin a manifest entry for a run."""
    return {"script": script, "command": [Path(sys.executable).name] + sys.argv, "args": vars(args),
            "started": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "git_commit": git_commit(), "versions": versions()}


def append_run(out, entry):
    """Finish the entry and append it to <out>/run_manifest.json. Returns the manifest path."""
    entry["finished"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    path = Path(out) / "run_manifest.json"
    runs = json.loads(path.read_text()).get("runs", []) if path.exists() else []
    runs.append(entry)
    path.write_text(json.dumps({"runs": runs}, indent=2, default=str))
    return path
