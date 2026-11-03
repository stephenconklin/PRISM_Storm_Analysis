#!/usr/bin/env python3
"""
Build an organized, styled QGIS project from a PRISM Storm Analysis output folder.

Finds the outputs in --out by their standard names (prism_watershed.py, storm_total.py and its
uncertainty file, subbasin_totals.py, validate_stations.py, validate_runoff.py, validate_snow.py) and
writes <out>/QGIS/<folder name>.qgz with:
    - groups: Validation, Storm totals, Daily precipitation, Boundaries, Basemap
    - readable layer names with units and dates; provenance in each layer's metadata
    - one shared color scale for daily precipitation and one for storm totals, with
      class breaks set from the data; station difference classes set from the data
    - relative paths (the folder can be moved), a NAD83 UTM CRS for the basin,
      map themes, a basin bookmark, and the Temporal Controller set to the storm window

Runs under QGIS's own Python (PyQGIS). Started from another Python (for example the
conda environment), it relaunches itself with QGIS's Python: --qgis-python, the
QGIS_PYTHON environment variable, or on macOS the newest QGIS 3.x app in /Applications
(the LTR line), falling back to the newest QGIS. QGIS 4 opens projects saved by QGIS 3.44,
but QGIS 3 may not open projects saved by QGIS 4, so 3.x is the default target.

An existing project at the same path is replaced; the previous one is kept as .qgz.bak.

Usage examples:
    python build_qgis_project.py --out /path/to/San_Juan_Watershed
    python build_qgis_project.py --out data/huc14080104 --name Animas_Oct2026
"""
import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

STYLE_DIR = Path(__file__).resolve().parents[1] / "styles"
# Anaconda sets these to its own GDAL/PROJ/PDAL, which break QGIS's bundled libraries.
CLEAR_ENV = ("PROJ_DATA", "PROJ_LIB", "GDAL_DATA", "GDAL_DRIVER_PATH", "PDAL_DRIVER_PATH",
             "PYTHONPATH", "PYTHONHOME")
NICE = [0.1, 0.2, 0.5, 1, 2, 3, 5, 10, 15, 20, 25, 30, 40, 50, 75, 100, 150, 200, 250, 300, 400, 500, 750, 1000,
        1500, 2000, 3000, 5000, 7500, 10000]
PRECIP_COLORS = ["#ffffd9", "#edf8b1", "#c7e9b4", "#7fcdbb", "#41b6c4", "#1d91c0", "#225ea8", "#253494",
                 "#081d58"]
DIFF_COLORS = ["#8c510a", "#d8b365", "#f6e8c3", "#f5f5f5", "#c7eae5", "#5ab4ac", "#01665e"]
OSM_URL = "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0"


# ---------- pure helpers (no QGIS needed) ----------

def nice_breaks(max_value, min_value=0.0, n_max=8):
    """Upper class bounds for a precipitation scale covering min_value to max_value, on round numbers.

    The first bound is the largest round number at or below min_value (or 0.5-1 mm when the data
    reach zero); values below it are drawn transparent, so dry areas show the basemap. Classes are
    spread evenly on a log scale, which suits precipitation.
    """
    if not max_value or max_value <= 0:
        return [1]
    top = next((v for v in NICE if v >= max_value), NICE[-1])
    floor = max((v for v in NICE if v <= min_value), default=None) if min_value >= 1 else None
    cands = [v for v in NICE if v < top and (floor is None or v >= floor)]
    if not cands:  # the data sit within one round-number step: keep one colored class below the top
        cands = [max(v for v in NICE if v < top)] if top > NICE[0] else []
    if len(cands) + 1 <= n_max:
        return cands + [top]
    lo = floor if floor is not None else (0.1 if top <= 2 else 0.5 if top <= 20 else 1)
    targets = [math.exp(math.log(lo) + i * (math.log(top) - math.log(lo)) / (n_max - 1)) for i in range(n_max - 1)]
    picks = sorted({min(cands, key=lambda v: abs(math.log(v) - math.log(t))) for t in targets})
    return picks + [top]


def nice_round(x):
    """Round a positive number to 1, 2 or 5 times a power of ten (at least 0.5)."""
    if x <= 0.5:
        return 0.5
    p = 10 ** math.floor(math.log10(x))
    return next(m * p for m in (1, 2, 5, 10) if m * p >= x)


def diff_breaks(abs_diffs):
    """Symmetric class edges for PRISM-minus-gauge differences, from the data's spread.

    Returns [-c, -b, -a, a, b, c] with a < b < c taken from the 40th, 75th and 95th
    percentiles of |difference|, rounded to 1-2-5 numbers.
    """
    vals = sorted(abs(v) for v in abs_diffs if v is not None)
    if not vals:
        return [-25, -10, -3, 3, 10, 25]
    def pct(q):
        return vals[min(len(vals) - 1, int(q * (len(vals) - 1) + 0.5))]
    a = nice_round(pct(0.40))
    b = max(nice_round(pct(0.75)), nice_round(a * 1.5))
    c = max(nice_round(pct(0.95)), nice_round(b * 1.5))
    return [-c, -b, -a, a, b, c]


def tif_kind(stem):
    """What a GeoTIFF in the folder holds, from the words in its name: 'volume', 'percent' (of normal), 'rain',
    'snow', 'depth',
    or None for anything else (a DEM, hillshade, ...), which is not mapped."""
    words = set(re.split(r"[^a-z0-9]+", stem.lower()))
    if words & {"volume", "m3"}:
        return "volume"
    if "normal" in words or "pct" in words:
        return "percent"
    if "snow" in words:
        return "snow"
    if "rain" in words:
        return "rain"
    if words & {"depth", "total", "mm", "ppt", "precip"}:
        return "depth"
    return None


SNOW_COLORS = ["#f7fbff", "#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#4292c6", "#2171b5", "#08519c", "#08306b"]


def is_number(value):
    """True for real numbers; PyQGIS returns a NULL object (not None) for empty fields."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def utm_epsg(lon):
    """NAD83 / UTM zone EPSG code for a longitude in the conterminous US (zones 10-19)."""
    zone = int((lon + 180) // 6) + 1
    return 26900 + max(1, min(23, zone))


def day_label(day):
    return f"{day:%b} {day.day}"


def window_label(start, end):
    if start == end:
        return f"{day_label(start)}, {end.year}"
    if start.month == end.month:
        return f"{start:%b} {start.day}–{end.day}, {end.year}"
    return f"{day_label(start)} – {day_label(end)}, {end.year}"


def discover(out):
    """Find the standard outputs in a PRISM Storm Analysis folder.

    Only precipitation is mapped (not tmean etc.), at one resolution: 800m if present, else 4km;
    "other_res" lists resolutions left out.
    """
    out = Path(out)
    have = sorted({m.group(1) for p in out.glob("clipped/ppt_*_clip.tif")
                   if (m := re.match(r"ppt_(800m|4km)_\d{8}_clip\.tif$", p.name))}
                  | {m.group(1) for p in out.glob("ppt_*_total_*.tif") if (m := re.match(r"ppt_(800m|4km)_", p.name))})
    res = "800m" if "800m" in have or not have else have[0]
    found = {"out": out, "res": res, "other_res": [r for r in have if r != res],
             "wbd": sorted(out.glob("wbd_huc*.geojson")) or sorted(out.glob("boundary.geojson")),
             "totals": sorted(out.glob(f"ppt_{res}_total_*.tif")),
             "daily": sorted(out.glob(f"clipped/ppt_{res}_*_clip.tif")),
             "summary": out / ("daily_basin_summary.csv" if res == "800m" else f"ppt_{res}_daily_basin_summary.csv"),
             "gpkg": out / "validation" / "validation.gpkg",
             "totals_gpkg": out / "totals.gpkg",
             "other_tifs": [p for p in sorted(out.glob("*.tif")) if "_total_" not in p.name]}  # volume, rain, snow
    dates = []
    for p in found["daily"]:
        m = re.search(r"_(\d{8})_clip\.tif$", p.name)
        if m:
            dates.append(dt.datetime.strptime(m.group(1), "%Y%m%d").date())
    found["days"] = dict(zip(dates, [p for p in found["daily"] if re.search(r"_(\d{8})_clip\.tif$", p.name)]))
    return found


def volume_range_km3(out, start, end, res=None, volume_m3=None):
    """(low, high) km³ from storm_total.py's uncertainty file for the window and resolution, or None.

    Without res, the file whose volume matches volume_m3 (within 0.1%) is used, so a label never shows
    another resolution's range.
    """
    span = f"{str(start).replace('-', '')}_{str(end).replace('-', '')}"
    files = sorted(Path(out).glob(f"ppt_{res}_uncertainty_{span}.json" if res else f"ppt_*_uncertainty_{span}.json"))
    for f in files:
        u = json.loads(f.read_text())
        if res or (volume_m3 and abs(u["volume_m3"] - volume_m3) <= 1e-3 * abs(volume_m3)):
            lo, hi = u["range_m3"]
            return lo / 1e9, hi / 1e9
    return None


def total_window(path):
    """(start, end) dates from a ppt_<res>_total_<start>_<end>.tif name, or None."""
    m = re.search(r"_total_(\d{8})_(\d{8})", Path(path).name)
    return tuple(dt.datetime.strptime(m.group(i), "%Y%m%d").date() for i in (1, 2)) if m else None


def primary_total(totals, start=None, end=None):
    """The event total to show: the one for the window (start, end) if given and present, else the longest."""
    spans = {p: total_window(p) for p in totals}
    for p, w in spans.items():
        if w and start and w == (start, end):
            return p
    dated = [p for p in totals if spans[p]]
    return max(dated, key=lambda p: (spans[p][1] - spans[p][0], spans[p][1])) if dated else (totals[0] if totals else None)


def releases(summary_csv):
    """{date: release} from daily_basin_summary.csv, if present."""
    if not Path(summary_csv).exists():
        return {}
    with open(summary_csv, newline="") as f:
        return {dt.date.fromisoformat(r["date"]): r.get("release", "") for r in csv.DictReader(f)}


# ---------- relaunch under QGIS's Python ----------

def version_key(text):
    """Sortable tuple from a version string like '4.2.3' or '3.44.10'."""
    return tuple(int(n) for n in re.findall(r"\d+", text or "")[:3])


def installed_qgis():
    """[(version, python path)] for QGIS apps in /Applications (macOS), newest first."""
    import plistlib
    found = []
    for app in glob.glob("/Applications/QGIS*.app"):
        py = Path(app) / "Contents" / "MacOS" / "python"
        try:
            with open(Path(app) / "Contents" / "Info.plist", "rb") as f:
                version = plistlib.load(f).get("CFBundleShortVersionString", "")
        except OSError:
            version = ""
        if py.exists():
            found.append((version, str(py)))
    return sorted(found, key=lambda v: version_key(v[0]), reverse=True)


def pick_qgis(apps):
    """Prefer the newest QGIS 3.x (projects it saves open in QGIS 3.x and 4.x); else the newest."""
    v3 = [a for a in apps if version_key(a[0])[:1] == (3,)]
    if v3:
        return v3[0], "the newest QGIS 3 installed, so the project opens in QGIS 3 and 4"
    return (apps[0], "the newest installed") if apps else (None, "")


def find_qgis_python(explicit=None):
    """--qgis-python, then QGIS_PYTHON, then the newest installed QGIS 3.x, else the newest QGIS."""
    if explicit:
        return explicit, "from --qgis-python"
    if os.environ.get("QGIS_PYTHON"):
        return os.environ["QGIS_PYTHON"], "from QGIS_PYTHON"
    app, why = pick_qgis(installed_qgis())
    return (app[1], f"QGIS {app[0]}, {why}") if app else (None, "")


def relaunch(argv, qgis_python):
    py, why = find_qgis_python(qgis_python)
    if py:
        print(f"Using {py} ({why}).", flush=True)
    if not py:
        sys.exit("PyQGIS is not available here and no QGIS Python was found. Pass --qgis-python or set "
                 "QGIS_PYTHON (on macOS: /Applications/QGIS-<version>.app/Contents/MacOS/python).")
    env = {k: v for k, v in os.environ.items() if k not in CLEAR_ENV}
    env.update(QT_QPA_PLATFORM="offscreen", PRISM_STORM_IN_QGIS="1")
    sys.exit(subprocess.run([py, str(Path(__file__).resolve()), *argv], env=env).returncode)


# ---------- QGIS ----------

def build(a):
    """Start QGIS, build the project, and shut QGIS down after every layer object is gone.

    Errors are caught and printed here, and the traceback dropped, before QGIS exits: a live traceback
    keeps _build_project's layers alive past exitQgis(), which segfaults.
    """
    import traceback
    from qgis.core import QgsApplication, QgsProject
    app = QgsApplication([], False)
    app.initQgis()
    code = 0
    try:
        _build_project(a)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        if not isinstance(e.code, int) and e.code is not None:
            print(e.code, file=sys.stderr)
    except BaseException:
        traceback.print_exc()
        code = 1
    QgsProject.instance().clear()
    app.exitQgis()
    if code:
        sys.exit(code)


def _build_project(a):
    from qgis.core import (Qgis, QgsApplication, QgsBookmark, QgsColorRampShader, QgsCoordinateReferenceSystem,
                           QgsCoordinateTransform, QgsDateTimeRange, QgsLayerTreeModel, QgsLayerMetadata,
                           QgsMapThemeCollection, QgsMarkerSymbol, QgsFillSymbol, QgsProject, QgsProperty,
                           QgsRasterLayer, QgsRasterShader, QgsReferencedRectangle, QgsRuleBasedRenderer,
                           QgsSingleBandPseudoColorRenderer, QgsSingleSymbolRenderer, QgsStyle, QgsSymbolLayer,
                           QgsVectorLayer, QgsInterval, QgsGraduatedSymbolRenderer, QgsRendererRange,
                           QgsPalLayerSettings, QgsVectorLayerSimpleLabeling, QgsTextFormat,
                           QgsTextBufferSettings, QgsTextBackgroundSettings)
    from qgis.PyQt.QtGui import QFont
    from qgis.PyQt.QtCore import QDateTime, QDate, QTime, Qt, QSizeF
    from qgis.PyQt.QtGui import QColor

    from osgeo import gdal
    gdal.UseExceptions()

    f = discover(a.out)
    out = f["out"].resolve()
    if not f["days"] and not f["totals"]:
        sys.exit(f"No PRISM Storm Analysis outputs found in {out}.")
    if f["other_res"]:
        print(f"Mapping {f['res']} precipitation; {', '.join(f['other_res'])} outputs in the folder are left out.")
    name = a.name or out.name
    qdir = out / "QGIS"
    qdir.mkdir(exist_ok=True)
    qgz = qdir / f"{name}.qgz"
    if qgz.exists():
        shutil.copy2(qgz, qgz.with_suffix(".qgz.bak"))

    proj = QgsProject.instance()
    proj.clear()
    proj.setFileName(str(qgz))
    proj.setFilePathStorage(Qgis.FilePathType.Relative)
    managed = []

    def tag(lyr, role, abstract):
        lyr.setCustomProperty("prism_storm/role", role)
        md = QgsLayerMetadata(lyr.metadata())
        md.setAbstract(abstract)
        lyr.setMetadata(md)
        managed.append(lyr)
        return lyr

    days = sorted(f["days"])
    start, end = (days[0], days[-1]) if days else (None, None)
    rel = releases(f["summary"])
    when = window_label(start, end) if start else ""

    # ---- study basin and CRS ----
    basin_lyr, basin_name, huc = None, name, ""
    if f["wbd"]:
        wbd = f["wbd"][0]
        m = re.search(r"wbd_huc(\d+)", wbd.name)
        huc = m.group(1) if m else ""
        basin_lyr = QgsVectorLayer(str(wbd), "basin", "ogr")
        feat = next(basin_lyr.getFeatures(), None)
        if feat is not None and "name" in basin_lyr.fields().names():
            basin_name = str(feat["name"])
    title = f"{basin_name}" + (f" (HUC {huc})" if huc else "") + (f" — PRISM days {when}" if when else "")
    proj.setTitle(title)
    ref = basin_lyr if basin_lyr is not None else QgsRasterLayer(str(f["totals"][0] if f["totals"]
                                                                     else f["daily"][0]), "ref")
    ext_ll = QgsCoordinateTransform(ref.crs(), QgsCoordinateReferenceSystem("EPSG:4269"), proj) \
        .transformBoundingBox(ref.extent())
    crs = QgsCoordinateReferenceSystem(f"EPSG:{utm_epsg(ext_ll.center().x())}")
    proj.setCrs(crs)

    root = proj.layerTreeRoot()
    top = root.addGroup(title)
    g_val = top.addGroup("Validation")
    g_tot = top.addGroup("Storm totals")
    g_day = top.addGroup("Daily precipitation")
    g_bnd = top.addGroup("Boundaries")
    g_base = top.addGroup("Basemap")

    def add(lyr, group, visible=True):
        proj.addMapLayer(lyr, False)
        node = group.addLayer(lyr)
        node.setItemVisibilityChecked(visible)
        return lyr

    # ---- precipitation rasters ----
    def raster_minmax(path):
        ds = gdal.Open(str(path))  # keep the dataset referenced while its band is read
        return ds.GetRasterBand(1).ComputeRasterMinMax(False)


    def precip_renderer(lyr, breaks, colors=PRECIP_COLORS):
        items = []
        lo = 0.0
        for i, hi in enumerate(breaks):
            color = QColor(0, 0, 0, 0) if i == 0 else QColor(colors[min(i - 1, len(colors) - 1)])
            items.append(QgsColorRampShader.ColorRampItem(hi, color, f"{lo:g}–{hi:g} mm" if i else f"< {hi:g} mm"))
            lo = hi
        fn = QgsColorRampShader(0, breaks[-1])
        fn.setColorRampType(Qgis.ShaderInterpolationMethod.Discrete)
        fn.setColorRampItemList(items)
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(fn)
        renderer = QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, shader)
        lyr.setRenderer(renderer)

    def percent_renderer(lyr, vmax):
        """Diverging classes around 100% of normal: brown below, teal above (DIFF_COLORS, low to high)."""
        edges = [50, 80, 120, 200, 400, max(800, nice_round(vmax))]
        labels = ["< 50%", "50–80%", "80–120% (near normal)", "120–200%", "200–400%", f"400–{edges[-1]:g}%"]
        colors = [DIFF_COLORS[0], DIFF_COLORS[1], DIFF_COLORS[3], DIFF_COLORS[4], DIFF_COLORS[5], DIFF_COLORS[6]]
        items = [QgsColorRampShader.ColorRampItem(e, QColor(c), lab) for e, c, lab in zip(edges, colors, labels)]
        fn = QgsColorRampShader(0, edges[-1])
        fn.setColorRampType(Qgis.ShaderInterpolationMethod.Discrete)
        fn.setColorRampItemList(items)
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(fn)
        lyr.setRenderer(QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, shader))

    rel_note = ("PRISM releases: " + ", ".join(f"{day_label(d)} r{rel[d]}" for d in days if rel.get(d))) if rel else ""
    totals = [QgsRasterLayer(str(p), p.stem) for p in f["totals"]]
    tranges = [raster_minmax(p) for p in f["totals"]]
    tbreaks = nice_breaks(max([r[1] for r in tranges], default=1), min([r[0] for r in tranges], default=0))
    # One event total is shown (the subbasin totals' window, else the longest); others are added switched off.
    basin_window = None
    if f["totals_gpkg"].exists():
        bt = QgsVectorLayer(f"{f['totals_gpkg']}|layername=basin_total", "bt", "ogr")
        ft = next(bt.getFeatures(), None) if bt.isValid() else None
        if ft is not None and "prism_start" in bt.fields().names() and ft["prism_start"]:
            basin_window = (dt.date.fromisoformat(str(ft["prism_start"])), dt.date.fromisoformat(str(ft["prism_end"])))
        del ft, bt
    shown = primary_total(f["totals"], *(basin_window or (None, None)))
    for p, t in zip(f["totals"], totals):
        s0, s1 = total_window(p) or (start, end)
        t.setName(f"Precipitation depth (mm), {window_label(s0, s1)}" if s0 else f"Precipitation depth (mm): {p.stem}")
        precip_renderer(t, tbreaks)
        tag(t, "storm_total_depth" if p == shown else "storm_total_other",
            f"PRISM storm-total precipitation (mm), PRISM days {s0} to {s1} (each ends 12:00 UTC). {rel_note}")
        add(t, g_tot, visible=p == shown)
    for p in f["other_tifs"]:  # storm_total.py --tif / --volume-tif / --rain-tif / --snow-tif outputs
        kind = tif_kind(p.stem)
        if kind is None:
            print(f"Not mapped (name doesn't say what it holds): {p.name}")
            continue
        lyr = QgsRasterLayer(str(p), p.stem)
        if kind == "percent":
            lyr.setName(f"Percent of 1991–2020 normal: {p.stem}")
            percent_renderer(lyr, raster_minmax(p)[1])
            tag(lyr, "percent_normal", "Storm total as a percent of PRISM's 1991-2020 normal for the same days "
                                       "(storm_context.py --pct-tif).")
            add(lyr, g_tot, visible=False)
            continue
        if kind in ("rain", "snow"):
            label = "Rain depth (mm)" if kind == "rain" else "Snow-water (mm)"
            lyr.setName(f"{label}: {p.stem}")
            precip_renderer(lyr, nice_breaks(max(raster_minmax(p)[1], 1)),
                            PRECIP_COLORS if kind == "rain" else SNOW_COLORS)
            tag(lyr, kind, f"Window-total {'rain' if kind == 'rain' else 'snow-water equivalent'} (mm) from "
                           "storm_total.py: PRISM precipitation split by PRISM daily mean temperature.")
            add(lyr, g_tot, visible=False)
            continue
        if kind == "volume":
            lyr.setName(f"Water volume per cell (m³): {p.stem}")
            vmin, vmax = raster_minmax(p)
            ramp = QgsStyle.defaultStyle().colorRamp("YlGnBu")
            fn = QgsColorRampShader(vmin, vmax, ramp,
                                    Qgis.ShaderInterpolationMethod.Linear, Qgis.ShaderClassificationMethod.Continuous)
            fn.classifyColorRamp(5, 1)
            shader = QgsRasterShader()
            shader.setRasterShaderFunction(fn)
            lyr.setRenderer(QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, shader))
            tag(lyr, "storm_total_volume", "Water volume per PRISM cell (m³) = depth × ellipsoidal cell area.")
        else:
            lyr.setName(f"Precipitation depth (mm): {p.stem}")
            precip_renderer(lyr, tbreaks)
            tag(lyr, "storm_total_window", "Storm-total depth for a window from storm_total.py --tif (mm).")
        add(lyr, g_tot, visible=False)

    dailies = [(d, QgsRasterLayer(str(f["days"][d]), f["days"][d].stem)) for d in days]
    dbreaks = nice_breaks(max([raster_minmax(l.source())[1] for _, l in dailies] + [1]))
    for d, lyr in reversed(dailies):  # latest day on top
        lyr.setName(f"Precipitation (mm), PRISM day {day_label(d)}")
        precip_renderer(lyr, dbreaks)
        tp = lyr.temporalProperties()
        tp.setMode(Qgis.RasterTemporalMode.FixedTemporalRange)
        t0 = QDateTime(QDate(d.year, d.month, d.day).addDays(-1), QTime(12, 0), Qt.TimeSpec.UTC)
        t1 = QDateTime(QDate(d.year, d.month, d.day), QTime(12, 0), Qt.TimeSpec.UTC)
        tp.setFixedTemporalRange(QgsDateTimeRange(t0, t1, True, False))
        tp.setIsActive(True)
        tag(lyr, "daily_precip", f"PRISM daily precipitation (mm) for the 24 h ending 12:00 UTC on {d}. "
                                 + (f"PRISM release {rel[d]}." if rel.get(d) else ""))
        add(lyr, g_day, visible=True)
    g_day.setItemVisibilityChecked(False)

    # ---- validation layers ----
    if f["gpkg"].exists():
        def vec(layer, label, role, abstract, visible):
            lyr = QgsVectorLayer(f"{f['gpkg']}|layername={layer}", label, "ogr")
            if not lyr.isValid():
                return None
            qml = STYLE_DIR / f"{layer}.qml"
            if qml.exists():
                msg, ok = lyr.loadNamedStyle(str(qml))
                if not ok:
                    print(f"Warning: style {qml.name} did not load: {msg}")
            lyr.setName(label)
            tag(lyr, role, abstract)
            add(lyr, g_val, visible)
            return lyr

        vec("gauges", "USGS gauges: runoff ratio, by regulation", "gauges",
            "USGS stream gauges in the study basin; status, regulation class and runoff ratio range "
            "from validate_runoff.py.", True)
        st = vec("stations", "Rain gauges: PRISM − gauge, storm total (mm)", "stations",
                 "PRISM minus gauge storm totals from validate_stations.py. Circles: PRISM inputs; "
                 "squares: independent gauges.", True)
        sd = vec("station_days", "Rain gauges: PRISM − gauge, daily (Temporal)", "station_days",
                 "PRISM minus gauge per PRISM day; animate with the Temporal Controller.", False)
        vec("dams", "Dams (NID), size = storage", "dams", "National Inventory of Dams in the study basin.", False)
        vec("gauge_basins", "Gauge drainage basins", "gauge_basins",
            "USGS NLDI drainage basins of compared gauges, colored by regulation.", False)
        sn = vec("snotel", "SNOTEL: PRISM − observed snow share (points)", "snotel",
                 "Snow as a share of precipitation over the window from validate_snow.py: PRISM split minus "
                 "SNOTEL SWE gain (percentage points). Size = SWE gain. Gray: skipped.", False)

        def restyle_stations(lyr, size_max):
            """Rule-based renderer like styles/stations.qml, with classes set from this run's data."""
            diffs = [ft["diff_mm"] for ft in lyr.getFeatures() if ft["in_stats"] and is_number(ft["diff_mm"])]
            obs = [ft["obs_mm"] for ft in lyr.getFeatures() if ft["in_stats"] and is_number(ft["obs_mm"])]
            edges = diff_breaks(diffs)
            top_obs = nice_round(max(obs)) if obs else size_max
            root_rule = QgsRuleBasedRenderer.Rule(None)
            bounds = [None] + edges + [None]
            for i, color in enumerate(DIFF_COLORS):
                lo, hi = bounds[i], bounds[i + 1]
                conds = ['"in_stats"'] + ([f'"diff_mm" >= {lo}'] if lo is not None else []) \
                    + ([f'"diff_mm" < {hi}'] if hi is not None else [])
                label = (f"< {hi:g}" if lo is None else f">= {lo:g}" if hi is None else f"{lo:g} to {hi:g}") + " mm"
                sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": color, "outline_color": "40,40,40",
                                                    "outline_width": "0.3", "size": "3"})
                sl = sym.symbolLayer(0)
                sl.setDataDefinedProperty(QgsSymbolLayer.Property.Name, QgsProperty.fromExpression(
                    "CASE WHEN \"in_sample\" THEN 'circle' ELSE 'square' END"))
                sl.setDataDefinedProperty(QgsSymbolLayer.Property.Size, QgsProperty.fromExpression(
                    f'scale_linear(coalesce("obs_mm", 0), 0, {top_obs}, 2.2, 6)'))
                root_rule.appendChild(QgsRuleBasedRenderer.Rule(sym, 0, 0, " AND ".join(conds), label))
            gray = QgsMarkerSymbol.createSimple({"name": "circle", "color": "0,0,0,0", "outline_color": "130,130,130",
                                                 "outline_width": "0.3", "size": "1.8"})
            root_rule.appendChild(QgsRuleBasedRenderer.Rule(gray, 0, 0, 'NOT "in_stats"',
                                                            "Not compared (missing data or day mismatch)"))
            lyr.setRenderer(QgsRuleBasedRenderer(root_rule))

        def restyle_snotel(lyr):
            """Diverging colors on PRISM-minus-observed snow share: brown = PRISM less snow, teal = more.
            Stations without both shares (skipped, or no tmean for the PRISM split) are gray."""
            fields = lyr.fields().names()
            need = {"prism_snow_pct", "observed_snow_pct", "swe_gain_mm"}
            diffs = ([ft["prism_snow_pct"] - ft["observed_snow_pct"] for ft in lyr.getFeatures()
                      if is_number(ft["prism_snow_pct"]) and is_number(ft["observed_snow_pct"])]
                     if need <= set(fields) else [])
            gains = [ft["swe_gain_mm"] for ft in lyr.getFeatures() if is_number(ft["swe_gain_mm"])] \
                if "swe_gain_mm" in fields else []
            top = nice_round(max(gains)) if gains else 100
            edges = [max(-100, min(100, e)) for e in diff_breaks(diffs)]  # shares differ by at most 100 points
            expr = '("prism_snow_pct" - "observed_snow_pct")'
            root_rule = QgsRuleBasedRenderer.Rule(None)
            bounds = [None] + edges + [None]
            for i, color in enumerate(DIFF_COLORS if diffs else []):
                lo, hi = bounds[i], bounds[i + 1]
                conds = [f"{expr} IS NOT NULL"] + ([f"{expr} >= {lo}"] if lo is not None else []) \
                    + ([f"{expr} < {hi}"] if hi is not None else [])
                label = (f"< {hi:g}" if lo is None else f">= {lo:g}" if hi is None else f"{lo:g} to {hi:g}") + " points"
                sym = QgsMarkerSymbol.createSimple({"name": "triangle", "color": color, "outline_color": "40,40,40",
                                                    "outline_width": "0.3", "size": "3"})
                sym.symbolLayer(0).setDataDefinedProperty(QgsSymbolLayer.Property.Size, QgsProperty.fromExpression(
                    f'scale_linear(coalesce("swe_gain_mm", 0), 0, {top}, 2.5, 6)'))
                root_rule.appendChild(QgsRuleBasedRenderer.Rule(sym, 0, 0, " AND ".join(conds), label))
            gray = QgsMarkerSymbol.createSimple({"name": "triangle", "color": "0,0,0,0", "outline_color": "130,130,130",
                                                 "outline_width": "0.3", "size": "2"})
            gray_rule = QgsRuleBasedRenderer.Rule(gray, 0, 0, "", "Skipped, or no PRISM split (no tmean)")
            gray_rule.setIsElse(True)
            root_rule.appendChild(gray_rule)
            lyr.setRenderer(QgsRuleBasedRenderer(root_rule))

        if sn is not None:
            restyle_snotel(sn)
        if st is not None:
            restyle_stations(st, 150)
        if sd is not None:
            restyle_stations(sd, 50)

    # ---- subbasin totals (subbasin_totals.py) ----
    def text_format(rgb, size, bold=False, box=False):
        fmt = QgsTextFormat()
        font = QFont("Arial")
        font.setBold(bold)
        fmt.setFont(font)
        fmt.setForcedBold(bold)
        fmt.setSize(size)
        fmt.setColor(QColor(*rgb))
        buf = QgsTextBufferSettings()
        buf.setEnabled(not box)
        buf.setSize(1.0)
        buf.setColor(QColor(255, 255, 255))
        fmt.setBuffer(buf)
        if box:  # white box with an outline in the text color
            bg = QgsTextBackgroundSettings()
            bg.setEnabled(True)
            bg.setType(QgsTextBackgroundSettings.ShapeRectangle)
            bg.setFillColor(QColor(255, 255, 255, 230))
            bg.setStrokeColor(QColor(*rgb))
            bg.setStrokeWidth(0.4)
            bg.setSize(QSizeF(1.5, 1.0))
            fmt.setBackground(bg)
        return fmt

    def label(lyr, expression, rgb, size, bold=False, box=False, priority=5, outside_top_left=False):
        pal = QgsPalLayerSettings()
        pal.fieldName = expression
        pal.isExpression = True
        pal.priority = priority
        if outside_top_left:
            # Pin the label just above the top-left corner of the feature's bounding box: always
            # outside the polygon, so it never covers the data inside it.
            P = getattr(QgsPalLayerSettings, "Property", QgsPalLayerSettings)
            props = pal.dataDefinedProperties()
            props.setProperty(P.PositionX, QgsProperty.fromExpression("x_min($geometry)"))
            props.setProperty(P.PositionY, QgsProperty.fromExpression("y_max($geometry)"))
            props.setProperty(P.Hali, QgsProperty.fromValue("Left"))
            props.setProperty(P.Vali, QgsProperty.fromValue("Bottom"))
            props.setProperty(P.AlwaysShow, QgsProperty.fromValue(True))
            pal.setDataDefinedProperties(props)
        pal.multilineAlign = Qgis.LabelMultiLineAlignment.Center
        pal.setFormat(text_format(rgb, size, bold, box))
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        lyr.setLabelsEnabled(True)

    totals_lyr = None
    if f["totals_gpkg"].exists():
        sub = QgsVectorLayer(f"{f['totals_gpkg']}|layername=subbasins", "subbasins", "ogr")
        totals_lyr = QgsVectorLayer(f"{f['totals_gpkg']}|layername=basin_total", "basin_total", "ogr")
        if sub.isValid() and sub.featureCount():
            feats = list(sub.getFeatures())
            level = feats[0]["level"]
            depths = [ft["mean_mm"] for ft in feats if is_number(ft["mean_mm"])] or [0.0]
            bounds = nice_breaks(max(depths), min(depths))
            ranges, lo = [], min(0.0, min(depths))
            for i, hi in enumerate(bounds):
                sym = QgsFillSymbol.createSimple({"color": PRECIP_COLORS[min(i, len(PRECIP_COLORS) - 1)],
                                                  "outline_color": "120,120,120", "outline_width": "0.2"})
                ranges.append(QgsRendererRange(lo, hi, sym, f"{lo:g}–{hi:g} mm"))
                lo = hi
            sub.setRenderer(QgsGraduatedSymbolRenderer("mean_mm", ranges))
            label(sub, "CASE WHEN \"rank\" <= 12 THEN \"name\" || '\\n' || format_number(\"volume_acre_ft\", 0) "
                       "|| ' acre-ft (' || format_number(\"share_pct\", 1) || '%)' END", (20, 40, 90), 8)
            sub.setMapTipTemplate(
                "<b>[% \"name\" %]</b> ([% \"level\" %] [% \"huc\" %])<br>"
                "Mean depth [% format_number(\"mean_mm\", 1) %] mm (max [% format_number(\"max_mm\", 1) %] mm)<br>"
                "Volume [% format_number(\"volume_acre_ft\", 0) %] acre-ft = [% format_number(\"volume_m3\", 0) %] m³<br>"
                "[% format_number(\"share_pct\", 2) %]% of the basin total; rank [% \"rank\" %]<br>"
                "Area with PRISM data [% format_number(\"area_km2\", 1) %] km² "
                "([% format_number(\"coverage_pct\", 1) %]% coverage)")
            sub.setName(f"Precipitation by subbasin ({level}): depth (color), volume (labels)")
            tag(sub, "subbasin_totals", f"Mean storm precipitation depth (fill) and water volume (labels, top 12 "
                                        f"by volume) for each {level} in the basin, from subbasin_totals.py.")
            add(sub, g_tot, visible=False)

    # ---- boundary and basemap ----
    if totals_lyr is not None and totals_lyr.isValid() and totals_lyr.featureCount():
        basin_lyr = totals_lyr  # same polygon, with the storm totals as attributes
    if basin_lyr is not None:
        basin_lyr.setName(f"Study basin: {basin_name}" + (f" (HUC {huc})" if huc else ""))
        if "volume_km3" in basin_lyr.fields().names():
            t = next(basin_lyr.getFeatures())
            vr = None
            if is_number(t["volume_km3"]) and is_number(t["volume_acre_ft"]) and is_number(t["mean_mm"]):
                af = t["volume_acre_ft"]
                af_text = f"{af / 1e6:.2f} million acre-ft" if af >= 1e6 else f"{af:,.0f} acre-ft"
                res = str(t["res"]) if "res" in basin_lyr.fields().names() and t["res"] else None
                vr = volume_range_km3(out, t["prism_start"], t["prism_end"], res, t["volume_m3"])
                range_text = f" (range {vr[0]:.2f}–{vr[1]:.2f})" if vr else ""
                basin_lyr.setName(f"{basin_lyr.name()}: {t['volume_km3']:.2f} km³{range_text} ({af_text}), "
                                  f"mean {t['mean_mm']:.0f} mm")
            del t
        outline = QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_color": "230,60,180",
                                              "outline_width": "0.8"})
        basin_lyr.setRenderer(QgsSingleSymbolRenderer(outline))
        if "volume_km3" in basin_lyr.fields().names():
            label(basin_lyr,
                  "\"name\" || ': ' || format_number(\"volume_km3\", 2) || ' km³ = ' || "
                  "CASE WHEN \"volume_acre_ft\" >= 1000000 THEN format_number(\"volume_acre_ft\" / 1000000, 2) "
                  "|| ' million acre-ft' ELSE format_number(\"volume_acre_ft\", 0) || ' acre-ft' END || '\\n' || "
                  "'mean ' || format_number(\"mean_mm\", 0) || ' mm, max ' || format_number(\"max_mm\", 0) "
                  "|| ' mm, PRISM days ' || \"prism_start\" || ' to ' || \"prism_end\" || "
                  "CASE WHEN \"coverage_pct\" < 99.5 THEN '\\n(' || format_number(100 - \"coverage_pct\", 1) "
                  "|| '% of the basin has no PRISM data)' ELSE '' END"
                  + (f" || '\\nrange {vr[0]:.2f}–{vr[1]:.2f} km³ (uncertainty components: storm_total.py)'" if vr else ""),
                  (150, 20, 110), 11, bold=True, box=True,
                  priority=10, outside_top_left=True)
            basin_lyr.setMapTipTemplate(
                "<b>[% \"name\" %]</b><br>Storm total: [% format_number(\"volume_km3\", 3) %] km³ = "
                "[% format_number(\"volume_acre_ft\", 0) %] acre-ft<br>Mean [% \"mean_mm\" %] mm, max "
                "[% \"max_mm\" %] mm over [% format_number(\"area_km2\", 0) %] km² with PRISM data<br>"
                "PRISM days [% \"prism_start\" %] to [% \"prism_end\" %]")
        tag(basin_lyr, "study_basin", "USGS Watershed Boundary Dataset polygon used to clip PRISM"
            + ("; attributes and label carry the storm totals from subbasin_totals.py."
               if "volume_km3" in basin_lyr.fields().names() else "."))
        add(basin_lyr, g_bnd, True)
    osm = QgsRasterLayer(OSM_URL, "OpenStreetMap", "wms")
    if osm.isValid():
        tag(osm, "basemap", "OpenStreetMap tiles © OpenStreetMap contributors.")
        add(osm, g_base, True)

    # ---- view, bookmark, temporal, themes ----
    view = QgsCoordinateTransform(ref.crs(), crs, proj).transformBoundingBox(ref.extent())
    view.scale(1.08)
    view.setYMaximum(view.yMaximum() + 0.12 * view.height())  # room for the basin label above the basin
    proj.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(view, crs))
    bookmark = QgsBookmark()
    bookmark.setName(f"Study basin: {basin_name}")
    bookmark.setExtent(QgsReferencedRectangle(view, crs))
    proj.bookmarkManager().addBookmark(bookmark)
    if start:
        ts = proj.timeSettings()
        ts.setTemporalRange(QgsDateTimeRange(
            QDateTime(QDate(start.year, start.month, start.day).addDays(-1), QTime(12, 0), Qt.TimeSpec.UTC),
            QDateTime(QDate(end.year, end.month, end.day), QTime(12, 0), Qt.TimeSpec.UTC)))
        ts.setTimeStep(1)
        ts.setTimeStepUnit(Qgis.TemporalUnit.Days)

    model = QgsLayerTreeModel(root)
    themes = proj.mapThemeCollection()
    default = {lyr.id(): root.findLayer(lyr.id()).itemVisibilityChecked() for lyr in managed
               if root.findLayer(lyr.id())}
    group_default = {g: g.itemVisibilityChecked() for g in (g_val, g_tot, g_day, g_bnd, g_base)}

    def theme(theme_name, roles, groups_on):
        for lyr in managed:
            node = root.findLayer(lyr.id())
            if node:
                node.setItemVisibilityChecked(lyr.customProperty("prism_storm/role") in roles)
        for g in (g_val, g_tot, g_day, g_bnd, g_base):
            g.setItemVisibilityChecked(g in groups_on)
        themes.insert(theme_name, QgsMapThemeCollection.createThemeFromCurrentState(root, model))

    theme("Storm total", {"storm_total_depth", "study_basin", "basemap"}, {g_tot, g_bnd, g_base})
    theme("Validation", {"gauges", "stations", "storm_total_depth", "study_basin", "basemap"},
          {g_val, g_tot, g_bnd, g_base})
    theme("Rivers and dams", {"gauges", "gauge_basins", "dams", "study_basin", "basemap"},
          {g_val, g_bnd, g_base})
    theme("Volume by subbasin", {"subbasin_totals", "study_basin", "basemap"}, {g_tot, g_bnd, g_base})
    theme("Rain and snow", {"rain", "snow", "snotel", "study_basin", "basemap"}, {g_tot, g_val, g_bnd, g_base})
    theme("Percent of normal", {"percent_normal", "study_basin", "basemap"}, {g_tot, g_bnd, g_base})
    theme("Daily animation (Temporal Controller)", {"daily_precip", "station_days", "study_basin", "basemap"},
          {g_day, g_val, g_bnd, g_base})
    for lyr in managed:
        node = root.findLayer(lyr.id())
        if node and lyr.id() in default:
            node.setItemVisibilityChecked(default[lyr.id()])
    for g, v in group_default.items():
        g.setItemVisibilityChecked(v)

    if not proj.write(str(qgz)):
        sys.exit(f"Could not write {qgz}")
    print(f"QGIS project: {qgz} (saved with QGIS {Qgis.version()})")
    print(f"  {len(managed)} layers; CRS {crs.authid()} ({crs.description()}); "
          f"themes: {', '.join(themes.mapThemes())}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="folder the PRISM Storm Analysis tools wrote to")
    ap.add_argument("--name", help="project name (default: the folder's name)")
    ap.add_argument("--qgis-python", help="QGIS's Python, if PyQGIS isn't importable here")
    a = ap.parse_args()
    try:
        import qgis.core  # noqa: F401
    except ImportError:
        if os.environ.get("PRISM_STORM_IN_QGIS"):
            sys.exit("PyQGIS could not be imported even in QGIS's Python.")
        relaunch(sys.argv[1:], a.qgis_python)
    build(a)


if __name__ == "__main__":
    main()
