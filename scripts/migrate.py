"""
Move an output folder made before 7 Nov 2026 (layout 1: everything at the top level) to layout 2
(layout.py): basin/, daily/, events/<event>/, qgis/, run/, with the national PRISM data in the shared store.
Run through `pipeline.py migrate --out DIR [--dry-run]`.

Offline and by renaming: nothing is downloaded or fetched again, outputs keep their contents, and steps the
pipeline had finished stay finished (their saved fingerprints are carried over to the new paths). Nothing
is deleted unless an exact copy stays: a "latest" copy of a per-window output that differs from the
window's own copy, a file whose window can't be told, and the original GeoPackages (split per window) are
kept in run/pre_migration/. Anything not recognized stays where it is and is listed.

Rerunning after an interruption continues where it stopped (every step checks what is already done).
"""
import csv
import datetime as dt
import filecmp
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import layout
from layout import Layout, move, name_window, parse_zip_stem, read_json, span

ROOT_DAILY = re.compile(r"^(?:(ppt|tmean|[a-z]+)_(800m|4km)_daily_clip\.nc|(?:[a-z]+_(?:800m|4km)_)?daily_basin_summary\.csv|"
                        r"[a-z]+_(?:800m|4km)_release_history\.csv)(?:\.aux\.xml)?$")
WINDOW_FILE = re.compile(r"^(?P<stem>(?:ppt|tmean)_(?:800m|4km)_[a-z_0-9]+?_(?P<s>\d{8})_(?P<e>\d{8}))(?P<ext>\.[a-z]+(?:\.aux\.xml)?)$")
PLAIN_VALIDATION = {"stations.csv": "stations_summary.json", "station_days.csv": "stations_summary.json",
                    "stations_summary.json": "stations_summary.json", "snotel.csv": "snotel_summary.json",
                    "snotel_summary.json": "snotel_summary.json", "runoff_summary.csv": None}
LAYER_SUMMARY = {"stations": "stations_summary.json", "station_days": "stations_summary.json",
                 "snotel": "snotel_summary.json"}


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def same_zip(a, b):
    """True if two zips hold the same members (names, sizes and CRC-32s), whatever their time stamps."""
    import zipfile
    try:
        with zipfile.ZipFile(a) as za, zipfile.ZipFile(b) as zb:
            key = lambda z: sorted((i.filename, i.file_size, i.CRC) for i in z.infolist())  # noqa: E731
            return key(za) == key(zb)
    except (OSError, zipfile.BadZipFile):
        return sha(a) == sha(b)


def iso8(ymd):
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"


def csv_window(path):
    """The (start, end) most rows of a CSV with prism_start/prism_end columns were made for, else None."""
    try:
        with open(path, newline="") as f:
            rows = [(r.get("prism_start"), r.get("prism_end")) for r in csv.DictReader(f)]
    except OSError:
        return None
    rows = [r for r in rows if r[0] and r[1]]
    return max(set(rows), key=rows.count) if rows else None


def json_window(path):
    j = read_json(path) or {}
    w = j.get("window") or j.get("prism_window") or j.get("run_key") or {}
    return (str(w["start"]), str(w["end"])) if w.get("start") and w.get("end") else None


class Plan:
    """The moves for one folder: built without touching anything (--dry-run prints it), then carried out."""

    def __init__(self, out, store=None):
        self.out = Path(out)
        self.lay = Layout(self.out, store=store)
        self.s = self.lay.settings
        self.ops = []          # (action, src, dst, note)
        self.unknown = []      # paths left in place
        self.windows = set()   # event windows that get a folder
        self.targets = set()   # new paths already planned

    # ---- recording ----
    def add(self, action, src, dst=None, note=""):
        if action == "move" and dst is not None:
            if Path(dst) in self.targets:  # two old files for one new name (e.g. two web copies of a report)
                self.keep_aside(src, f"{Path(dst).relative_to(self.out)} comes from another file")
                return
            self.targets.add(Path(dst))
        self.ops.append((action, Path(src), Path(dst) if dst else None, note))

    def event_dir(self, w):
        self.windows.add(w)
        return self.lay.event_dir(*w)

    def keep_aside(self, src, note):
        self.add("aside", src, self.lay.run_dir / "pre_migration" / Path(src).relative_to(self.out), note)

    def download_span(self):
        d = self.s.get("download") or {}
        if not (d.get("start") and d.get("end")):
            return None
        end = min(layout.iso(d["end"]), (dt.date.today() - dt.timedelta(days=1)).isoformat())
        return span(layout.iso(d["start"]), end)

    # ---- building ----
    def build(self):
        out, lay = self.out, self.lay
        lock = out / ".lock"
        if lock.exists():
            import runtime
            info = runtime.lock_holder(out, lock)
            if info:
                raise SystemExit(f"{out} is in use by another run (pid {info.get('pid')}): {info.get('command')}")
            self.add("delete", lock, note="lock left by a run that is gone")
        names = sorted(os.listdir(out))
        dl_span = self.download_span()
        import config
        referenced = config.boundary_path(self.s) if self.s.get("boundary") else None
        for name in names:
            p = out / name
            if name in ("storm.toml", ".DS_Store", "basin", "daily", "events", "qgis", "run") and name != "QGIS":
                continue
            if name in ("raw_zips", "extracted", "normals_zips", "normals"):
                getattr(self, f"_store_{name}")(p)
            elif referenced and p.resolve() == Path(referenced).resolve():
                # storm.toml's boundary: it stays where storm.toml says; the scripts' copy goes to basin/
                if name == "boundary.geojson" or name.endswith((".geojson", ".gpkg", ".shp")):
                    self.add("copy", p, lay.boundary_copy, "storm.toml's boundary stays; a copy for the scripts")
            elif re.match(r"^wbd_huc\d+\.geojson$|^boundary\.geojson$", name):
                self.add("move", p, lay.basin_dir / name)
            elif name == "clipped":
                self.add("merge", p, lay.clipped_dir)
            elif ROOT_DAILY.match(name):
                self.add("move", p, lay.daily_dir / name)
            elif (m := WINDOW_FILE.match(name)):
                w = (iso8(m.group("s")), iso8(m.group("e")))
                kind_total = re.match(r"^[a-z]+_(?:800m|4km)_total_", name)
                if kind_total and dl_span == f"{m.group('s')}_{m.group('e')}":
                    self.add("move", p, lay.daily_dir / name, "the download window's total")
                else:
                    self.add("move", p, self.event_dir(w) / "totals" / name)
            elif name == "totals.gpkg":
                self._split_gpkg(p, "subbasins")
            elif name == "subbasin_totals.csv":
                w = csv_window(p)
                if w:
                    self.add("move", p, self.event_dir(w) / "subbasins" / f"subbasin_totals_{span(*w)}.csv")
                else:
                    self.keep_aside(p, "window unknown")
            elif name == "validation":
                self._validation(p)
            elif name == "report":
                self._report(p)
            elif name == "QGIS":
                self.add("rename_dir", p, lay.qgis_dir)
            elif name == "logs":
                self.add("merge", p, lay.logs_dir)
            elif name == "pipeline_state.json":
                self.add("state", p, lay.state_file)
            elif name == "run_manifest.json" or name.startswith("run_manifest.corrupt-"):
                self.add("move", p, lay.run_dir / name)
            elif name == ".lock":
                continue
            elif name.endswith(".part"):
                self.add("delete", p, note="left by an interrupted write")
            else:
                self.unknown.append(p)
        return self

    def _store_raw_zips(self, d):
        for z in sorted(d.rglob("*")):
            if z.is_dir():
                continue
            if z.suffix == ".part" or z.name.endswith(".zip.part"):
                self.add("delete", z, note="interrupted download")
                continue
            base = z.name.split(".zip")[0]
            arch = re.match(r"^(prism_.+)_r(\d+|unknown)$", base) if z.parent.name == "releases" else None
            info = parse_zip_stem(arch.group(1) if arch else base)
            if info is None:
                self.unknown.append(z)
                continue
            zdir = self.lay.store.zips_dir(info["var"], info["res"], info["region"])
            self.add("store_zip", z, (zdir / "releases" if arch else zdir) / z.name)

    def _store_extracted(self, d):
        for f in sorted(d.iterdir()):
            if f.name.startswith(".unzip-"):
                self.add("delete", f, note="interrupted unzip")
                continue
            info = parse_zip_stem(f.name) if f.is_dir() else None
            if info is None:
                self.unknown.append(f)
                continue
            release = layout.folder_release(f)
            self.add("store_dir", f, self.lay.store.extracted_dir(info["var"], info["res"], info["region"])
                     / f"{f.name}_r{layout.release_int(release)}")

    def _normals_target(self, name):
        m = re.match(r"^prism_([a-z]+)_([a-z]+)_(\d+[a-z])_", name)
        if not m or m.group(3) not in layout.RES_FROM_CODE:
            return None
        return self.lay.store.normals_dir(m.group(1), layout.RES_FROM_CODE[m.group(3)], m.group(2))

    def _store_normals_zips(self, d):
        for z in sorted(d.iterdir()):
            t = self._normals_target(z.name)
            if t is None or z.suffix != ".zip":
                (self.unknown.append(z) if not z.name.endswith(".part") else self.add("delete", z))
                continue
            self.add("store_zip", z, t / z.name)

    def _store_normals(self, d):
        for f in sorted(d.iterdir()):
            t = self._normals_target(f.name)
            if t is None or not f.is_dir():
                self.unknown.append(f)
                continue
            self.add("store_dir", f, t / f.name)

    def _validation(self, d):
        summaries = {n: json_window(d / n) for n in ("stations_summary.json", "snotel_summary.json")}
        plain_runoff = {}
        for f in sorted(d.iterdir()):
            m = re.match(r"^runoff_(\w+?)\.json$", f.name)
            if m and not re.search(r"_\d{8}_\d{8}$", m.group(1)):
                plain_runoff[m.group(1)] = json_window(f)
        for f in sorted(d.iterdir()):
            name = f.name
            if f.is_dir():
                self.unknown.append(f)
            elif name == "validation.gpkg":
                self._split_gpkg(f, "validation", summaries)
            elif (m := re.match(r"^runoff_(\w+)_basin\.geojson$", name)):
                self.add("move", f, self.lay.gauge_basin(m.group(1)))
            elif (m := re.match(r"^runoff_(\w+?)_flow_(\d{8})_(\d{8})\.csv$", name)):
                w = (iso8(m.group(2)), iso8(m.group(3)))
                self.add("move", f, self.event_dir(w) / "validation" / "runoff" / name)
            elif (m := re.match(r"^runoff_(\w+?)_flow\.csv$", name)):
                w = plain_runoff.get(m.group(1))
                self._plain(f, w, "validation/runoff", f"runoff_{m.group(1)}_flow")
            elif (m := re.match(r"^runoff_(\w+?)_(\d{8})_(\d{8})\.json$", name)):
                w = (iso8(m.group(2)), iso8(m.group(3)))
                self.add("move", f, self.event_dir(w) / "validation" / "runoff" / name)
            elif (m := re.match(r"^runoff_(\w+?)\.json$", name)):
                self._plain(f, plain_runoff.get(m.group(1)), "validation/runoff", f"runoff_{m.group(1)}")
            elif (w := name_window(Path(name).stem)) and Path(name).stem.rsplit("_", 2)[0] + Path(name).suffix in PLAIN_VALIDATION:
                self.add("move", f, self.event_dir(w) / "validation" / name)
            elif name in PLAIN_VALIDATION:
                key = PLAIN_VALIDATION[name]
                w = summaries.get(key) if key else csv_window(f)
                self._plain(f, w, "validation", Path(name).stem)
            elif name.endswith(".part"):
                self.add("delete", f, note="left by an interrupted write")
            else:
                self.unknown.append(f)

    def _plain(self, f, w, sub, stem):
        """A 'latest' copy: dropped if the window's own copy is identical, moved in if there is none, else kept aside."""
        if not w:
            self.keep_aside(f, "window unknown")
            return
        name = f"{stem}_{span(*w)}{f.suffix}"
        sibling = f.parent / name
        target = self.event_dir(w) / sub / name
        if sibling.exists() or target.exists():
            twin = sibling if sibling.exists() else target
            if filecmp.cmp(f, twin, shallow=False):
                self.add("delete", f, note=f"same as {name}")
            else:
                self.keep_aside(f, f"differs from {name}")
        else:
            self.add("move", f, target, "the latest copy, named for its window")

    def _report(self, d):
        for f in sorted(p for p in d.rglob("*") if p.is_file()):
            rel = f.relative_to(d)
            name = f.name
            if name == ".DS_Store":
                self.add("delete", f)
                continue
            m = re.search(r"_(\d{8})_(\d{8})(?:_(web|\d{8}-\d{6}))?\.(html|svg)$", name)
            if not m:
                self.unknown.append(f)
                continue
            w = (iso8(m.group(1)), iso8(m.group(2)))
            rdir = self.event_dir(w) / "report"
            sp = span(*w)
            if rel.parts[0] == "history":
                self.add("move", f, rdir / "history" / name)
            elif rel.parts[0] in ("figures", "web"):
                self.add("move", f, rdir / rel)
            elif m.group(3) == "web":
                self.add("move", f, rdir / "web" / f"storm_report_{sp}.html")
            else:
                self.add("move", f, rdir / name)

    def _split_gpkg(self, gpkg, kind, summaries=None):
        """Plan the layers of a layout 1 GeoPackage into each window's GeoPackage (dams to basin/dams.gpkg)."""
        from build_qgis_project import gpkg_layers, gpkg_window
        layers = sorted(gpkg_layers(gpkg))
        plan = []
        windowed = {(n.rsplit("_", 2)[0], name_window(n)) for n in layers if name_window(n)}
        for n in layers:
            w = name_window(n)
            if n == "dams":
                plan.append((n, self.lay.dams_gpkg, "dams"))
                continue
            if w:
                base = n.rsplit("_", 2)[0]
            else:
                base = n
                summary = LAYER_SUMMARY.get(n)
                w = (summaries or {}).get(summary) if summary else None
                if w is None:
                    g = gpkg_window(gpkg, n)
                    w = (g[0].isoformat(), g[1].isoformat()) if g else None
                if w is None:
                    continue  # kept in the original, set aside below
                if (base, w) in windowed:
                    continue  # the window's own layer is there
            target = self.event_dir(w) / kind / ("totals.gpkg" if kind == "subbasins" else "validation.gpkg")
            plan.append((n, target, f"{base}_{span(*w)}"))
        self.add("split_gpkg", gpkg, None, json.dumps([(a, str(b), c) for a, b, c in plan]))
        self.keep_aside(gpkg, "the original, split per window")

    # ---- report ----
    def summary(self):
        counts = {}
        for action, *_ in self.ops:
            counts[action] = counts.get(action, 0) + 1
        return counts

    def print(self, verbose=False):
        rel = lambda p: (str(p.relative_to(self.out)) if p and str(p).startswith(str(self.out)) else str(p))  # noqa: E731
        print(f"Migrate {self.out} to the folder layout of 7 Nov 2026; PRISM store: {self.lay.store.root}")
        for action, src, dst, note in self.ops:
            if not verbose and action in ("store_zip", "store_dir"):
                continue
            if action == "split_gpkg":
                for layer, target, new in json.loads(note):
                    print(f"  layer      {rel(src)}:{layer} -> {rel(Path(target))}:{new}")
                continue
            print(f"  {action:<10} {rel(src)}" + (f" -> {rel(dst)}" if dst else "") + (f"  ({note})" if note else ""))
        n_store = sum(a in ("store_zip", "store_dir") for a, *_ in self.ops)
        if n_store and not verbose:
            print(f"  ... and {n_store} national zips and unzipped grids into the store (--verbose lists them)")
        if self.unknown:
            print("Not recognized (left where they are):")
            for p in self.unknown:
                print(f"  {rel(p)}")
        print("Event folders: " + (", ".join(sorted(f"{self.lay.event_dir(*w).name} ({w[0]} to {w[1]})"
                                                   for w in self.windows)) or "none"))

    # ---- carrying out ----
    def execute(self):
        done = {"moved": 0, "deleted": 0, "store_deduplicated": 0, "set_aside": 0, "layers": 0}
        for w in sorted(self.windows):
            self.lay.prepare_event(*w)
        for action, src, dst, note in self.ops:
            if action != "split_gpkg" and not src.exists():
                continue  # done in an earlier, interrupted migration
            if action == "move":
                if dst.exists() and src.is_file() and filecmp.cmp(src, dst, shallow=False):
                    src.unlink()
                    done["deleted"] += 1
                    continue
                if dst.exists():  # differs from what is already there: keep both
                    aside = self.lay.run_dir / "pre_migration" / src.relative_to(self.out)
                    move(src, aside if not aside.exists() else aside.with_name(aside.name + ".2"))
                    done["set_aside"] += 1
                    continue
                move(src, dst)
                done["moved"] += 1
            elif action == "merge":
                layout.merge_tree(src, dst) if dst.exists() else move(src, dst)
                done["moved"] += 1
            elif action == "rename_dir":
                if dst.exists() and not os.path.samefile(src, dst):  # (QGIS and qgis are one folder on macOS)
                    layout.merge_tree(src, dst)
                else:  # QGIS -> qgis: a case-only rename needs a step between on a case-insensitive disk
                    tmp = src.with_name(src.name + ".migrating")
                    os.replace(src, tmp)
                    os.replace(tmp, dst)
                done["moved"] += 1
            elif action == "copy":
                if not dst.exists():
                    if src.suffix == ".geojson":
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                    else:  # another format: the scripts read boundary.geojson
                        import geopandas as gpd
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        gpd.read_file(src).to_crs(4269).dissolve().to_file(dst, driver="GeoJSON")
                done["moved"] += 1
            elif action == "delete":
                shutil.rmtree(src) if src.is_dir() else src.unlink()
                done["deleted"] += 1
            elif action == "aside":
                move(src, dst if not dst.exists() else dst.with_name(dst.name + f".{dt.datetime.now():%Y%m%d%H%M%S}"))
                done["set_aside"] += 1
            elif action == "store_zip":
                done["store_deduplicated" if self._store_zip(src, dst) else "moved"] += 1
            elif action == "store_dir":
                if dst.exists():
                    shutil.rmtree(src)
                    done["store_deduplicated"] += 1
                else:
                    move(src, dst)
                    done["moved"] += 1
            elif action == "split_gpkg":
                done["layers"] += self._split(src, json.loads(note))
            elif action == "state":
                dst.parent.mkdir(parents=True, exist_ok=True)
                move(src, dst)
                done["moved"] += 1
        for name in ("raw_zips", "extracted", "normals_zips", "normals", "validation", "report"):
            d = self.out / name
            if d.is_dir():
                for sub in sorted((p for p in d.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
                try:
                    d.rmdir()
                except OSError:
                    pass
        return done

    def _store_zip(self, src, dst):
        """Move a zip into the store. Returns True if the store already had it (the folder's copy is removed).
        Two zips with the same members (names, sizes, CRC-32) hold the same data even if their bytes differ
        (zip time stamps). Two different releases of a day: the newer stays current, the older is archived.
        The same release with different data: the folder's copy is kept in run/pre_migration/."""
        def aside():
            target = self.lay.run_dir / "pre_migration" / src.relative_to(self.out)
            move(src, target if not target.exists() else target.with_name(target.name + ".2"))
        if not dst.exists():
            move(src, dst)
            return False
        if os.path.samefile(src, dst) or same_zip(src, dst):
            src.unlink()
            return True
        if dst.parent.name == "releases" or "normals" in dst.parts:
            aside()
            return False
        mine, theirs = layout.release_int(layout.zip_release(src)), layout.release_int(layout.zip_release(dst))
        if mine == theirs:
            aside()
            return False
        older, newer = (src, dst) if mine < theirs else (dst, src)
        arch = self.lay.store.archived_zip(dst, layout.release_int(layout.zip_release(older)))
        if arch.exists() and same_zip(arch, older):
            older.unlink()
        elif arch.exists():
            if older == src:
                aside()
            else:
                move(older, arch.with_name(arch.stem + f"_{sha(older)[:8]}.zip"))
        else:
            move(older, arch)
        if newer == src:
            move(src, dst)
        return False

    def _split(self, gpkg, plan):
        """Copy each planned layer into its new GeoPackage, with its style from the original."""
        import geopandas as gpd
        from validate_stations import STYLE_DIR, store_style
        styles = {}
        try:
            con = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
            styles = {r[0]: r[1] for r in con.execute("SELECT f_table_name, styleQML FROM layer_styles WHERE useAsDefault")}
            con.close()
        except sqlite3.Error:
            pass
        n = 0
        for layer, target, new in plan:
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            gdf = gpd.read_file(gpkg, layer=layer)
            gdf.to_file(target, layer=new, driver="GPKG")
            base = new.rsplit("_", 2)[0] if name_window(new) else new
            qml = styles.get(layer) or ((STYLE_DIR / f"{base}.qml").read_text() if (STYLE_DIR / f"{base}.qml").exists()
                                        else None)
            if qml:
                store_style(target, new, qml)
            n += 1
        return n


def carry_over_state(out):
    """Give the pipeline's finished steps fingerprints for their new paths, when the fingerprint they had matches
    the same step's arguments at the old paths (so a step that was already out of date stays out of date)."""
    import pipeline
    lay = Layout(out)
    if not lay.state_file.exists():
        return 0
    state = json.loads(lay.state_file.read_text())
    c = pipeline.Ctx(out)
    events = {e[0]: e for e in c.events}
    steps = {s.name: s for s in pipeline.STEPS}
    events_root = str(lay.events_dir)
    kept = 0
    for key, st in state.get("steps", {}).items():
        if isinstance(st.get("log"), str) and st["log"].startswith("logs/"):
            st["log"] = "run/" + st["log"]
        name, _, ev_name = key.partition(":")
        step, ev = steps.get(name), events.get(ev_name) if ev_name else None
        if step is None or (ev_name and ev is None) or st.get("status") != "ok":
            continue
        args = step.args(c, ev)
        if isinstance(args, str):
            continue
        old_args = [str(out / Path(x).name) if x.startswith(events_root) else x for x in args]
        if st.get("fingerprint") == pipeline.fingerprint(c, step, old_args):
            st["fingerprint"] = pipeline.fingerprint(c, step, args)
            kept += 1
    import runtime
    runtime.write_json(lay.state_file, state)
    return kept


def rebuild_qgis(out, qgis_python=None):
    """Rebuild the QGIS project (its layers point at the old paths). Returns a note."""
    from build_qgis_project import find_qgis_python
    if not find_qgis_python(qgis_python)[0]:
        return "QGIS not found: rebuild the project later with build_qgis_project.py"
    names = [p.stem for p in Layout(out, settings={}).qgis_dir.glob("*.qgz")]
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "build_qgis_project.py"), "--out", str(out)]
    for name in names or [None]:
        res = subprocess.run(cmd + (["--name", name] if name else []) + (["--qgis-python", qgis_python] if qgis_python else []),
                             capture_output=True, text=True)
        if res.returncode:
            return f"QGIS project rebuild failed (exit {res.returncode}): {(res.stdout + res.stderr)[-600:]}"
    return "QGIS project rebuilt" + (f" ({', '.join(names)}.qgz; the old one is kept as .qgz.bak)" if names else "")
