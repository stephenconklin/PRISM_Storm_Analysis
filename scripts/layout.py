"""
Where everything lives: an output folder for one basin, and the PRISM store shared by every basin.
Standard library only, so every script (and QGIS's Python) can use it.

Layout 2 (since 7 Nov 2026; `pipeline.py migrate` moves a folder from layout 1, where everything sat at
the top of the folder):

    <out>/
      storm.toml
      basin/            wbd_huc<code>.geojson or boundary.geojson; gauges/<site>_basin.geojson (NLDI
                        basins of USGS gauges); dams.gpkg (NID dams in the study basin)
      daily/            clipped/<var>_<res>_<YYYYMMDD>_clip.tif, <var>_<res>_daily_clip.nc, the daily
                        summaries and release histories, and the download window's total
      events/<name>/    one storm window: an event in storm.toml, else events/<start>_<end>/
        event.json      the window and event name
        totals/         depth, volume, rain, snow and percent-of-normal rasters; uncertainty and normals JSON
        subbasins/      totals.gpkg, subbasin_totals_<start>_<end>.csv
        validation/     station, SNOTEL and runoff tables and summaries, validation.gpkg; runoff/ per gauge
        report/         storm_report_<start>_<end>.html, figures/, history/, web/
      qgis/             <name>.qgz
      run/              pipeline_state.json, run_manifest.json, logs/, .lock

    <store>/            prism_store in storm.toml, else PRISM_STORE, else <out>/../prism_store
      zips/<region>/<res>/<var>/<zip_name>         the current release of each day; older ones in releases/
      extracted/<region>/<res>/<var>/<stem>_r<n>/  unzipped grids, one folder per day and release
      normals/<region>/<res>/<var>/                30-year normal zips and their unzipped grids

Files inside an event folder keep their window in their name (_<YYYYMMDD>_<YYYYMMDD>), so a file copied
out of the folder still says which storm it covers, and GeoPackage layers keep window-named tables.

The store is keyed by release: a refresh in one basin adds a release without changing what another basin
reads. Each basin reads the release its clipped days carry; older releases are unzipped again on demand
from the archived zips.
"""
import datetime as dt
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

VERSION = 2
# PRISM file names carry a resolution code instead of the resolution: 30s = 800 m, 25m = 4 km.
RES_CODES = {"800m": "30s", "4km": "25m"}
RES_FROM_CODE = {v: k for k, v in RES_CODES.items()}
# Names at the top of a layout 1 folder (anything here means the folder hasn't been migrated).
LEGACY_NAMES = {"clipped", "raw_zips", "extracted", "normals_zips", "validation", "logs", "QGIS",
                "run_manifest.json", "pipeline_state.json", "totals.gpkg"}
ZIP_RE = re.compile(r"^prism_(?P<var>[a-z]+)_(?P<region>[a-z]+)_(?P<code>\d+[a-z])_(?P<ymd>\d{8})"
                    r"(?:_(?P<fmt>[a-z]+))?$")
EXTRACTED_RE = re.compile(r"^(?P<stem>prism_.+)_r(?P<release>\d+)$")
STALE_UNZIP_S = 3600  # a .unzip-* folder older than this was left by an interrupted run


# ---------- names ----------

def iso(d):
    return d.isoformat() if isinstance(d, (dt.date, dt.datetime)) else str(d)


def span(start, end):
    """'<YYYYMMDD>_<YYYYMMDD>' for a window: the suffix of every per-window file and layer name."""
    return f"{iso(start).replace('-', '')}_{iso(end).replace('-', '')}"


def ymd(day):
    return day.strftime("%Y%m%d") if isinstance(day, (dt.date, dt.datetime)) else str(day).replace("-", "")


def windowed_name(name, start, end):
    """stations.csv -> stations_<YYYYMMDD>_<YYYYMMDD>.csv"""
    p = Path(name)
    return f"{p.stem}_{span(start, end)}{p.suffix}"


SPAN_RE = re.compile(r"_(\d{8})_(\d{8})$")


def name_window(stem):
    """(start, end) ISO strings from a name ending in _<YYYYMMDD>_<YYYYMMDD>, else None."""
    m = SPAN_RE.search(stem)
    if not m:
        return None
    try:
        return tuple(dt.datetime.strptime(m.group(i), "%Y%m%d").date().isoformat() for i in (1, 2))
    except ValueError:
        return None


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("._") or None


def zip_name(var, res, region, day, fmt=""):
    """Name of one daily zip, unique per variable, resolution, region, date and format. Matches PRISM's own
    name for the default COG (prism_ppt_us_30s_20261020.zip); other formats get a suffix."""
    return f"prism_{var}_{region}_{RES_CODES[res]}_{ymd(day)}{f'_{fmt}' if fmt else ''}.zip"


def parse_zip_stem(stem):
    """{var, region, res, ymd, fmt} from a PRISM daily zip stem, or None."""
    m = ZIP_RE.match(stem)
    if not m or m.group("code") not in RES_FROM_CODE:
        return None
    return {"var": m.group("var"), "region": m.group("region"), "res": RES_FROM_CODE[m.group("code")],
            "ymd": m.group("ymd"), "fmt": m.group("fmt") or ""}


# ---------- PRISM zips and releases ----------

def parse_release(text):
    """PRISM_DATASET_RELEASE_NUMBER (1-8) from an info.txt, as a string ('' if absent)."""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "PRISM_DATASET_RELEASE_NUMBER":
            return value.strip()
    return ""


def zip_info(zpath):
    """Text of a PRISM zip's info.txt ('' if none)."""
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if "info" in Path(name).name and name.endswith(".txt"):
                return z.read(name).decode(errors="ignore")
    return ""


def zip_release(zpath):
    """Release number recorded in a PRISM zip's info.txt ('' if none)."""
    return parse_release(zip_info(zpath))


def folder_release(folder):
    """Release number in an extracted folder's info.txt ('' if none)."""
    for info in Path(folder).rglob("*info*.txt"):
        return parse_release(info.read_text(errors="ignore"))
    return ""


def release_int(release):
    return int(release) if str(release).isdigit() else 0


# ---------- small file helpers ----------

def write_text_atomic(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def move(src, dst):
    """Move a file or folder (a rename on the same volume), creating dst's parent."""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:  # another volume, or a non-empty folder in the way
        shutil.move(str(src), str(dst))


def merge_tree(src, dst):
    """Move every file under src into dst at the same relative path; where both have a file, the newer one
    is kept. src is removed. event.json files are not carried over (the caller writes dst's)."""
    src, dst = Path(src), Path(dst)
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = f.relative_to(src)
        if rel == Path("event.json"):
            f.unlink()
            continue
        target = dst / rel
        if target.exists():
            if f.stat().st_mtime > target.stat().st_mtime:
                os.replace(f, target)
            else:
                f.unlink()
        else:
            move(f, target)
    shutil.rmtree(src, ignore_errors=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


# ---------- the store ----------

class Store:
    """National PRISM data shared by every basin folder: zips, unzipped grids, normals."""

    def __init__(self, root):
        self.root = Path(root)

    def zips_dir(self, var, res, region="us"):
        return self.root / "zips" / region / res / var

    def zip_path(self, var, res, region, day, fmt=""):
        return self.zips_dir(var, res, region) / zip_name(var, res, region, day, fmt)

    def archived_zip(self, zpath, release):
        """releases/<stem>_r<release>.zip beside a zip: where a superseded release is kept."""
        zpath = Path(zpath)
        return zpath.parent / "releases" / f"{zpath.stem}_r{release or 'unknown'}.zip"

    def extracted_dir(self, var, res, region="us"):
        return self.root / "extracted" / region / res / var

    def extracted_day(self, zpath, release, var=None, res=None, region=None):
        """Folder holding one day's unzipped grid at one release."""
        stem = Path(zpath).stem
        if var is None:
            p = parse_zip_stem(stem)
            var, res, region = p["var"], p["res"], p["region"]
        return self.extracted_dir(var, res, region) / f"{stem}_r{release_int(release)}"

    def normals_dir(self, var, res, region="us"):
        return self.root / "normals" / region / res / var

    def clean_leftovers(self, var, res, region="us"):
        """Remove .unzip-* folders that interrupted unzips left behind (only old ones: another basin may be
        unzipping into the same store right now)."""
        d = self.extracted_dir(var, res, region)
        if not d.exists():
            return
        for p in d.glob(".unzip-*"):
            try:
                if time.time() - p.stat().st_mtime > STALE_UNZIP_S:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass

    def extract(self, zpath, release=None):
        """Unzip a day's zip into extracted/.../<stem>_r<release>/ (once). Returns the folder."""
        zpath = Path(zpath)
        p = parse_zip_stem(zpath.stem.rsplit("_r", 1)[0] if zpath.parent.name == "releases" else zpath.stem)
        if p is None:
            raise ValueError(f"Not a PRISM daily zip name: {zpath.name}")
        stem = zip_name(p["var"], p["res"], p["region"], dt.datetime.strptime(p["ymd"], "%Y%m%d").date(),
                        p["fmt"])[:-4]
        release = zip_release(zpath) if release is None else release
        dest = self.extracted_dir(p["var"], p["res"], p["region"]) / f"{stem}_r{release_int(release)}"
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Unzip into a temporary folder and rename it into place, so an interrupted unzip never leaves a
        # partial grid under the day's name.
        tmp = Path(tempfile.mkdtemp(prefix=".unzip-", dir=dest.parent))
        try:
            with zipfile.ZipFile(zpath) as z:
                z.extractall(tmp)
            try:
                tmp.rename(dest)
            except OSError:
                if not dest.exists():  # not just another process finishing first
                    raise
                shutil.rmtree(tmp, ignore_errors=True)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return dest

    def prune_extracted(self, zpath, keep_release):
        """Remove unzipped folders of older releases of a day (their zips stay in releases/, and a basin
        that still uses one unzips it again on demand)."""
        zpath = Path(zpath)
        p = parse_zip_stem(zpath.stem)
        if p is None:
            return
        for d in self.extracted_dir(p["var"], p["res"], p["region"]).glob(f"{zpath.stem}_r*"):
            m = EXTRACTED_RE.match(d.name)
            if m and m.group("stem") == zpath.stem and int(m.group("release")) < release_int(keep_release):
                shutil.rmtree(d, ignore_errors=True)

    def day_folders(self, var, res, region, day):
        """{(fmt, release): folder} of the unzipped grids for one day."""
        found = {}
        d = self.extracted_dir(var, res, region)
        if not d.exists():
            return found
        for f in d.glob(f"prism_{var}_{region}_{RES_CODES[res]}_{ymd(day)}*_r*"):
            m = EXTRACTED_RE.match(f.name)
            p = parse_zip_stem(m.group("stem")) if m else None
            if p and f.is_dir():
                found[(p["fmt"], int(m.group("release")))] = f
        return found

    def day_folder(self, var, res, region, day, release=None):
        """The unzipped folder for a day at release (the newest when None), unzipping the current or an
        archived zip when it isn't unzipped yet. None if the store has neither."""
        def pick(folders):
            if not folders:
                return None
            if release:
                hits = [k for k in folders if k[1] == release_int(release)]
                return folders[min(hits, key=lambda k: k[0] != "")] if hits else None
            return folders[max(folders, key=lambda k: (k[1], k[0] == ""))]
        got = pick(self.day_folders(var, res, region, day))
        if got is not None:
            return got
        zdir = self.zips_dir(var, res, region)
        for fmt in ("", "nc", "bil", "asc"):
            z = zdir / zip_name(var, res, region, day, fmt)
            candidates = [z] if z.exists() else []
            if release:
                arch = self.archived_zip(z, release_int(release))
                candidates = ([arch] if arch.exists() else []) + candidates
            for c in candidates:
                try:
                    rel = zip_release(c)
                except (OSError, zipfile.BadZipFile):
                    continue
                if not release or release_int(rel) == release_int(release):
                    return self.extract(c, rel)
        return None


def store_dir(out, settings=None, override=None):
    """The store for a folder: override (a --store flag), prism_store in storm.toml (relative to it),
    PRISM_STORE, the older prism_cache / PRISM_CACHE names, else prism_store next to the folder."""
    s = settings or {}
    for value, base in ((override, Path.cwd()),
                        (s.get("prism_store") or s.get("prism_cache"),
                         Path(s["_path"]).parent if s.get("_path") else Path.cwd()),
                        (os.environ.get("PRISM_STORE") or os.environ.get("PRISM_CACHE"), Path.cwd())):
        if value:
            p = Path(value).expanduser()
            return p if p.is_absolute() else (base / p).resolve()
    return Path(out).resolve().parent / "prism_store"


# ---------- the basin folder ----------

def legacy(out):
    """True if out holds a layout 1 folder (outputs at its top level) that hasn't been migrated."""
    try:
        names = set(os.listdir(out))
    except OSError:
        return False
    return bool(names & LEGACY_NAMES) or any(n.endswith("_daily_clip.nc") for n in names)


def migrate_hint(out):
    return (f"{out} uses the old folder layout (everything at the top level). Move it to the new layout first "
            f"(offline, renames only):\n  python scripts/pipeline.py migrate --out {out} --dry-run\n"
            f"  python scripts/pipeline.py migrate --out {out}")


def _settings(out):
    try:
        import config
        return config.load(out)
    except Exception:  # unreadable settings are reported by the script that needs them
        return None


class Layout:
    """Paths in one basin folder (layout 2) and its store. Windows are (start, end) dates or ISO strings."""

    def __init__(self, out, settings=None, store=None):
        self.out = Path(out)
        self.settings = (settings if settings is not None else _settings(self.out)) or {}
        try:
            import config
            self.events = config.events(self.settings)
        except Exception:
            self.events = []
        self.region = self.settings.get("region", "us")
        self.store = Store(store_dir(self.out, self.settings, store))
        self.settings_file = self.out / "storm.toml"
        self.basin_dir = self.out / "basin"
        self.gauges_dir = self.basin_dir / "gauges"
        self.dams_gpkg = self.basin_dir / "dams.gpkg"
        self.daily_dir = self.out / "daily"
        self.clipped_dir = self.daily_dir / "clipped"
        self.events_dir = self.out / "events"
        self.qgis_dir = self.out / "qgis"
        self.run_dir = self.out / "run"
        self.state_file = self.run_dir / "pipeline_state.json"
        self.manifest_file = self.run_dir / "run_manifest.json"
        self.logs_dir = self.run_dir / "logs"
        self.lock_file = self.run_dir / ".lock"

    # ---- basin ----
    def wbd(self, huc):
        return self.basin_dir / f"wbd_huc{huc}.geojson"

    @property
    def boundary_copy(self):
        return self.basin_dir / "boundary.geojson"

    def boundary_files(self):
        """The basin polygon file(s): wbd_huc*.geojson, else boundary.geojson."""
        return sorted(self.basin_dir.glob("wbd_huc*.geojson")) or sorted(self.basin_dir.glob("boundary.geojson"))

    def gauge_basin(self, site):
        return self.gauges_dir / f"{site}_basin.geojson"

    # ---- daily ----
    def clip_tif(self, var, res, day):
        return self.clipped_dir / f"{var}_{res}_{ymd(day)}_clip.tif"

    def cube(self, var, res):
        return self.daily_dir / f"{var}_{res}_daily_clip.nc"

    def summary_csv(self, var, res):
        """daily_basin_summary.csv for 800 m precipitation (its original name), else <var>_<res>_daily_basin_summary.csv."""
        return self.daily_dir / ("daily_basin_summary.csv" if (var, res) == ("ppt", "800m")
                                 else f"{var}_{res}_daily_basin_summary.csv")

    def release_history(self, var, res):
        return self.daily_dir / f"{var}_{res}_release_history.csv"

    def download_total(self, var, res, start, end):
        """prism_watershed.py's total over the days it downloaded (not a storm window)."""
        return self.daily_dir / f"{var}_{res}_total_{span(start, end)}.tif"

    # ---- events ----
    def event_name(self, start, end):
        """The storm.toml event whose window is start..end, else None."""
        s, e = iso(start), iso(end)
        return next((n for n, a, b in self.events if (a, b) == (s, e)), None)

    def _named_dir(self, start, end):
        name = self.event_name(start, end)
        return self.events_dir / (safe_name(name) if name and safe_name(name) else span(start, end))

    def event_dirs(self):
        return sorted(p for p in self.events_dir.iterdir() if p.is_dir()) if self.events_dir.exists() else []

    def dir_window(self, d):
        """(start, end) ISO strings an event folder holds: its event.json, else its name, else None."""
        w = (read_json(Path(d) / "event.json") or {})
        if w.get("start") and w.get("end"):
            return iso(w["start"]), iso(w["end"])
        return name_window(Path(d).name)

    def windows(self):
        """[(start, end)] of every event folder."""
        return [w for w in (self.dir_window(d) for d in self.event_dirs()) if w]

    def event_dir(self, start, end):
        """The folder for a window (for reading; prepare_event makes it)."""
        s, e = iso(start), iso(end)
        target = self._named_dir(s, e)
        if self.dir_window(target) == (s, e) or not target.exists():
            for d in [target] + self.event_dirs():
                if d.exists() and self.dir_window(d) == (s, e):
                    return d
        return target

    def prepare_event(self, start, end):
        """Make (and tidy) the folder for a window before writing to it. If the event's folder holds another
        window (its dates changed in storm.toml), that window moves to events/<start>_<end>/; other folders
        holding this window (an earlier run without the event, or under the event's old name) merge in."""
        s, e = iso(start), iso(end)
        name = self.event_name(s, e)
        target = self._named_dir(s, e)
        held = self.dir_window(target) if target.exists() else None
        if held and held != (s, e):
            aside = self.events_dir / span(*held)
            if aside != target:
                merge_tree(target, aside)
                self._write_event(aside, None, *held)
        for d in self.event_dirs():
            if d != target and self.dir_window(d) == (s, e):
                merge_tree(d, target)
        target.mkdir(parents=True, exist_ok=True)
        info = read_json(target / "event.json") or {}
        if (info.get("name"), info.get("start"), info.get("end")) != (name, s, e):
            self._write_event(target, name, s, e)
        return target

    def _write_event(self, d, name, s, e):
        d.mkdir(parents=True, exist_ok=True)
        write_text_atomic(d / "event.json", json.dumps({"name": name, "start": s, "end": e, "layout": VERSION},
                                                       indent=2) + "\n")

    def totals_dir(self, start, end):
        return self.event_dir(start, end) / "totals"

    def subbasins_dir(self, start, end):
        return self.event_dir(start, end) / "subbasins"

    def validation_dir(self, start, end):
        return self.event_dir(start, end) / "validation"

    def runoff_dir(self, start, end):
        return self.validation_dir(start, end) / "runoff"

    def report_dir(self, start, end):
        return self.event_dir(start, end) / "report"

    def window_tif(self, kind, res, start, end):
        """ppt_<res>_<kind>_<start>_<end>.tif in the window's totals/ (kind: total, volume, rain, snow, pct_normal)."""
        return self.totals_dir(start, end) / f"ppt_{res}_{kind}_{span(start, end)}.tif"

    def uncertainty_json(self, res, start, end):
        return self.totals_dir(start, end) / f"ppt_{res}_uncertainty_{span(start, end)}.json"

    def normals_json(self, res, start, end):
        return self.totals_dir(start, end) / f"ppt_{res}_normals_{span(start, end)}.json"

    def totals_gpkg(self, start, end):
        return self.subbasins_dir(start, end) / "totals.gpkg"

    def subbasin_csv(self, start, end):
        return self.subbasins_dir(start, end) / f"subbasin_totals_{span(start, end)}.csv"

    def validation_gpkg(self, start, end):
        return self.validation_dir(start, end) / "validation.gpkg"

    def validation_file(self, name, start, end):
        """A validation table or summary for the window: validation/<stem>_<start>_<end>.<ext>."""
        return self.validation_dir(start, end) / windowed_name(name, start, end)

    def report_html(self, start, end):
        return self.report_dir(start, end) / f"storm_report_{span(start, end)}.html"

    # ---- all windows ----
    def all_totals(self, pattern):
        """Files matching pattern in every event's totals/ (sorted by name)."""
        return sorted((p for d in self.event_dirs() for p in (d / "totals").glob(pattern)), key=lambda p: p.name)
