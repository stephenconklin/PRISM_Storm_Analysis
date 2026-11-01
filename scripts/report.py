#!/usr/bin/env python3
"""
Storm report: one self-contained HTML page of charts and headline numbers from a PRISM Data
Wrangler output folder, plus each chart as a standalone SVG for slides and documents.

Charts (each skipped, with a note, when its inputs are missing or were made for another window):
    Storm timeline     basin precipitation per PRISM day (rain and snow with a tmean cube) above
                       15-minute discharge at up to three gauges, with the event runoff shaded
    PRISM vs gauges    storm totals at rain gauges (validate_stations.py), PRISM inputs vs independent
    Where the water went  runoff ratio per gauge (validate_runoff.py), all precipitation and rain only
    Volume and uncertainty  the storm volume and its uncertainty components (storm_total.py)
    Snow check         snow share at SNOTEL stations, observed vs PRISM split (validate_snow.py)

The window defaults to the latest storm_total.py run recorded in run_manifest.json, else every day
in the cube. Outputs: <out>/report/storm_report_<start>_<end>.html and
<out>/report/figures/<chart>_<start>_<end>.svg. The page has no external dependencies, follows the
viewer's light/dark setting, and has a data table under each chart.

Usage examples:
    python report.py --out data/huc1408
    python report.py --out data/huc1408 --start 2026-10-20 --end 2026-10-27 --gauges 09342500 09361500
"""
import argparse
import csv
import datetime as dt
import html
import json
import math
import sys
from pathlib import Path

import numpy as np
import xarray as xr

import manifest
from storm_total import M3_PER_ACRE_FT, partition_window, window_total

UTC = dt.timezone.utc
W = 760  # chart width (px); the page scales charts down on narrow screens

# Colors by role. Series 1-3 are the validated categorical slots (blue, orange, aqua), checked
# all-pairs in both modes; the light aqua is below 3:1 contrast, so every chart has a legend and a
# data table. Exported SVGs use the light values.
LIGHT = {"surface": "#fcfcfb", "page": "#f9f9f7", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
         "grid": "#e1e0d9", "axis": "#c3c2b7", "s1": "#2a78d6", "s2": "#eb6834", "s3": "#1baf7a",
         "s1soft": "#b7d3f6", "warning": "#fab219", "good": "#0ca30c", "critical": "#d03b3b"}
DARK = {"surface": "#1a1a19", "page": "#0d0d0d", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "axis": "#383835", "s1": "#3987e5", "s2": "#d95926", "s3": "#199e70",
        "s1soft": "#1c5cab", "warning": "#fab219", "good": "#0ca30c", "critical": "#d03b3b"}

CHART_CSS = """
.bg{fill:var(--surface)} .grid{stroke:var(--grid);stroke-width:1} .axis{stroke:var(--axis);stroke-width:1}
.t{fill:var(--ink2);font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
.tm{fill:var(--muted);font:11px system-ui,-apple-system,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
.th{fill:var(--ink);font:600 12px system-ui,-apple-system,"Segoe UI",sans-serif}
.f1{fill:var(--s1)} .f2{fill:var(--s2)} .f3{fill:var(--s3)} .fsoft{fill:var(--s1);fill-opacity:.22}
.l1{stroke:var(--s1);stroke-width:2;fill:none} .l2{stroke:var(--s2);stroke-width:2;fill:none}
.l3{stroke:var(--s3);stroke-width:2;fill:none} .lm{stroke:var(--muted);stroke-width:1;fill:none}
.ring{stroke:var(--surface);stroke-width:2} .hit{fill:transparent}
.o1{stroke:var(--s1);stroke-width:2;fill:var(--surface)} .o2{stroke:var(--s2);stroke-width:2;fill:var(--surface)}
.o3{stroke:var(--s3);stroke-width:2;fill:var(--surface)}
"""


# ---------- small helpers ----------

def esc(s):
    return html.escape(str(s), quote=True)


def fmt(v, nd=0):
    return "–" if v is None else f"{v:,.{nd}f}"


def num(v):
    """float from a CSV cell, or None for blanks."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def nice_step(span, n=5):
    raw = span / max(n, 1)
    p = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    return next(m * p for m in (1, 2, 2.5, 5, 10) if m * p >= raw)


def ticks(lo, hi, n=5):
    """Round tick values covering [lo, hi]; returns (ticks, new_lo, new_hi)."""
    if hi <= lo:
        hi = lo + 1
    step = nice_step(hi - lo, n)
    t0, t1 = math.floor(lo / step) * step, math.ceil(hi / step) * step
    out = [t0 + i * step for i in range(int(round((t1 - t0) / step)) + 1)]
    return out, t0, t1


def scale(d0, d1, r0, r1):
    k = (r1 - r0) / (d1 - d0) if d1 != d0 else 0
    return lambda v: r0 + (v - d0) * k


def top_rounded_bar(x, y, w, h, r=4):
    """Path for a bar anchored at the bottom with rounded top corners."""
    if h <= 0 or w <= 0:
        return ""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}H{x + w - r:.1f}"
            f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{y + h:.1f}Z")


def svg(width, height, body, label):
    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
            f'aria-label="{esc(label)}" xmlns="http://www.w3.org/2000/svg">'
            f'<rect class="bg" width="{width}" height="{height}"/>{body}</svg>')


def standalone(svg_text, colors=LIGHT):
    """An SVG file that renders on its own (light colors, no page CSS)."""
    css = ":root{" + ";".join(f"--{k}:{v}" for k, v in colors.items()) + "}" + CHART_CSS
    head, rest = svg_text.split(">", 1)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{head}><style>{css}</style>{rest}'


def legend(items, x, y):
    """items: [(class, label, shape)] with shape 'box' or 'line'; returns (svg, height)."""
    out, cx = [], x
    for cls, label, shape in items:
        if shape == "line":
            out.append(f'<line x1="{cx}" y1="{y - 4}" x2="{cx + 16}" y2="{y - 4}" class="{cls}"/>')
        elif shape == "ring":
            out.append(f'<circle cx="{cx + 6}" cy="{y - 4}" r="4" class="{cls}"/>')
        else:
            out.append(f'<rect x="{cx}" y="{y - 10}" width="12" height="12" rx="2" class="{cls}"/>')
        out.append(f'<text x="{cx + 20}" y="{y}" class="t">{esc(label)}</text>')
        cx += 30 + 7 * len(label)
    return "".join(out)


def table(headers, rows):
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f'<details><summary>Data table</summary><div class="tw"><table><tr>{head}</tr>{body}</table></div></details>'


# ---------- data ----------

def default_window(out, cube_days):
    """(start, end) of the latest storm_total.py run in the manifest, else the cube's full range."""
    path = Path(out) / "run_manifest.json"
    if path.exists():
        for run in reversed(json.loads(path.read_text()).get("runs", [])):
            if run.get("script") == "storm_total.py" and run.get("window"):
                return run["window"]["start"], run["window"]["end"]
    return str(min(cube_days)), str(max(cube_days))


def daily_basin(out, res, start, end):
    """Per PRISM day: basin precipitation and (with a tmean cube) rain and snow, in area-weighted mm."""
    with xr.open_dataset(Path(out) / f"ppt_{res}_daily_clip.nc") as ds:
        r = window_total(ds, start, end)
        inside = r["total_mm"].notnull()
        area = ds.cell_area.where(inside)
        a_sum = float(area.sum())

        def mean(da):
            return [float((da.isel(time=i) * area).sum()) / a_sum for i in range(da.sizes["time"])]
        days = [str(t)[:10] for t in r["daily_mm"].time.values]
        out_rows = {"days": days, "ppt": mean(r["daily_mm"]), "volume_m3": r["volume_m3"],
                    "area_m2": r["area_m2"], "mean_mm": r["mean_mm"], "max_mm": r["max_mm"],
                    "releases": r["releases"], "rain": None, "snow": None}
        part = partition_window(out, res, ds, r, 0.0, 3.0)
        if not isinstance(part, str):
            out_rows["rain"], out_rows["snow"] = mean(part["rain_mm"]), mean(part["snow_mm"])
    return out_rows


def latest_qc(out):
    """{script: worst QC level} from the newest manifest entry of each script."""
    path = Path(out) / "run_manifest.json"
    seen = {}
    if path.exists():
        order = {"skip": 0, "pass": 1, "warn": 2, "fail": 3}
        for run in reversed(json.loads(path.read_text()).get("runs", [])):
            s = run.get("script")
            if s in ("prism_watershed.py", "storm_total.py") and s not in seen and run.get("qc"):
                seen[s] = max((c["level"] for c in run["qc"]), key=order.get)
    return seen


def read_csv(path):
    return list(csv.DictReader(open(path, newline=""))) if Path(path).exists() else []


def pick_gauges(rows, wanted=None, n=3):
    """Gauges for the timeline: the ones asked for, else the n compared gauges not regulated by dams that
    carried the most event runoff (the storm's main rivers, not large dry basins)."""
    ok = [r for r in rows if r["status"] == "ok"]
    if wanted:
        return [r for w in wanted for r in ok if r["site"] == w]
    free = [r for r in ok if r.get("regulation") != "regulated"]
    return sorted(free, key=lambda r: -(num(r["runoff_const_m3"]) or 0))[:n]


# ---------- charts ----------

def timeline_chart(basin, gauges, flows):
    """Precipitation per PRISM day above one discharge panel per gauge, sharing a UTC time axis."""
    days = [dt.date.fromisoformat(d) for d in basin["days"]]
    t_lo = dt.datetime.combine(days[0] - dt.timedelta(days=1), dt.time(12), UTC)
    t_hi = dt.datetime.combine(days[-1], dt.time(12), UTC)
    for f in flows:
        if f:
            t_lo, t_hi = min(t_lo, f[0][0]), max(t_hi, f[-1][0])
    L, R, top = 64, 16, 28
    pw = W - L - R
    x = scale(t_lo.timestamp(), t_hi.timestamp(), L, L + pw)
    ph, gh, gap = 150, 110, 34
    height = top + ph + len(gauges) * (gh + gap) + (62 if gauges else 40)
    foot = height - (62 if gauges else 40) + 6  # baseline of the date labels
    body = []
    # Day gridlines and labels at 00:00 UTC.
    d = dt.datetime.combine(t_lo.date() + dt.timedelta(days=1), dt.time(0), UTC)
    while d <= t_hi:
        xv = x(d.timestamp())
        body.append(f'<line x1="{xv:.1f}" y1="{top}" x2="{xv:.1f}" y2="{foot - 12}" class="grid"/>')
        body.append(f'<text x="{xv:.1f}" y="{foot + 4}" class="tm" text-anchor="middle">{d:%b} {d.day}</text>')
        d += dt.timedelta(days=1)
    body.append(f'<text x="{L}" y="{foot + 20}" class="tm">Dates at 00:00 UTC; each precipitation bar is a PRISM day '
                '(12:00 UTC to 12:00 UTC).</text>')

    # Precipitation panel.
    has_split = basin["rain"] is not None
    tot = basin["ppt"]
    yt, _, ymax = ticks(0, max(tot) or 1, 4)
    y = scale(0, ymax, top + ph, top + 8)
    for v in yt:
        body.append(f'<line x1="{L}" y1="{y(v):.1f}" x2="{L + pw}" y2="{y(v):.1f}" class="grid"/>'
                    f'<text x="{L - 6}" y="{y(v) + 4:.1f}" class="tm" text-anchor="end">{v:g}</text>')
    body.append(f'<text x="{L}" y="{top - 10}" class="th">Basin precipitation (mm per PRISM day)</text>')
    for i, day in enumerate(days):
        x0 = x(dt.datetime.combine(day - dt.timedelta(days=1), dt.time(12), UTC).timestamp()) + 1
        x1 = x(dt.datetime.combine(day, dt.time(12), UTC).timestamp()) - 1
        w = x1 - x0
        if has_split:
            rain, snow = basin["rain"][i], basin["snow"][i]
            hr = y(0) - y(rain)
            hs = y(0) - y(rain + snow) - hr
            if snow > 0 and hs > 2.5:  # snow on top of rain, with a 2px surface gap
                body.append(f'<path d="{top_rounded_bar(x0, y(rain) - hs, w, hs - 2)}" class="f3"/>'
                            f'<rect x="{x0:.1f}" y="{y(rain):.1f}" width="{w:.1f}" height="{hr:.1f}" class="f1"/>')
            else:
                body.append(f'<path d="{top_rounded_bar(x0, y(rain + snow), w, y(0) - y(rain + snow))}" class="f1"/>')
            tip = f"{day:%b} {day.day}: {tot[i]:.1f} mm ({rain:.1f} rain, {snow:.1f} snow)"
        else:
            body.append(f'<path d="{top_rounded_bar(x0, y(tot[i]), w, y(0) - y(tot[i]))}" class="f1"/>')
            tip = f"{day:%b} {day.day}: {tot[i]:.1f} mm"
        body.append(f'<rect x="{x0:.1f}" y="{top}" width="{w:.1f}" height="{ph}" class="hit" data-tip="{esc(tip)}"/>')
    peak = int(np.argmax(tot))
    px = (x(dt.datetime.combine(days[peak] - dt.timedelta(days=1), dt.time(12), UTC).timestamp())
          + x(dt.datetime.combine(days[peak], dt.time(12), UTC).timestamp())) / 2
    body.append(f'<text x="{px:.1f}" y="{y(tot[peak]) - 6:.1f}" class="th" text-anchor="middle">{tot[peak]:.0f} mm</text>')
    body.append(f'<line x1="{L}" y1="{y(0):.1f}" x2="{L + pw}" y2="{y(0):.1f}" class="axis"/>')
    if has_split:
        body.append(legend([("f1", "Rain", "box"), ("f3", "Snow (water equivalent)", "box")], L + pw - 260, top - 10))

    # One discharge panel per gauge.
    for k, (g, f) in enumerate(zip(gauges, flows)):
        t0 = top + ph + gap + k * (gh + gap)
        name = g["name"].title()
        ratio = f"runoff ratio {num(g['ratio_low']):.2f}–{num(g['ratio_high']):.2f}"
        body.append(f'<text x="{L}" y="{t0 - 8}" class="th">{esc(name)} ({esc(g["site"])}), discharge (cfs); '
                    f'{esc(ratio)}</text>')
        if not f:
            body.append(f'<text x="{L}" y="{t0 + 20}" class="tm">No flow record saved for this gauge.</text>')
            continue
        qs = [p[1] for p in f]
        yt, _, qmax = ticks(0, max(qs), 3)
        yq = scale(0, qmax, t0 + gh, t0)
        for v in yt:
            body.append(f'<line x1="{L}" y1="{yq(v):.1f}" x2="{L + pw}" y2="{yq(v):.1f}" class="grid"/>'
                        f'<text x="{L - 6}" y="{yq(v) + 4:.1f}" class="tm" text-anchor="end">{v:,.0f}</text>')
        pts = [(x(t.timestamp()), yq(q), yq(b)) for t, q, b in f]  # (x, y of discharge, y of baseflow)
        # Event runoff: between the baseflow line and the discharge above it (smaller y = higher flow).
        area = ("M" + " L".join(f"{px:.1f},{min(qy, by):.1f}" for px, qy, by in pts) + " L" +
                " L".join(f"{px:.1f},{by:.1f}" for px, _, by in reversed(pts)) + "Z")
        body.append(f'<path d="{area}" class="fsoft"/>')
        body.append('<path d="M' + " L".join(f"{px:.1f},{by:.1f}" for px, _, by in pts) + '" class="lm"/>')
        body.append('<path d="M' + " L".join(f"{px:.1f},{qy:.1f}" for px, qy, _ in pts) + '" class="l1"/>')
        i = int(np.argmax(qs))
        body.append(f'<circle cx="{pts[i][0]:.1f}" cy="{pts[i][1]:.1f}" r="4" class="f1 ring"/>'
                    f'<text x="{pts[i][0] + 8:.1f}" y="{pts[i][1] + 4:.1f}" class="t">peak {qs[i]:,.0f} cfs, '
                    f'{f[i][0]:%b} {f[i][0].day} {f[i][0]:%H:%M} UTC</text>')
        step = max(1, len(f) // 120)
        for j in range(0, len(f), step):
            xa = pts[j][0]
            wv = (pts[min(j + step, len(f) - 1)][0] - xa) or 2
            body.append(f'<rect x="{xa:.1f}" y="{t0}" width="{wv:.1f}" height="{gh}" class="hit" '
                        f'data-tip="{esc(f"{f[j][0]:%b} {f[j][0].day} {f[j][0]:%H:%M} UTC: {f[j][1]:,.0f} cfs")}"/>')
        body.append(f'<line x1="{L}" y1="{yq(0):.1f}" x2="{L + pw}" y2="{yq(0):.1f}" class="axis"/>')
    if gauges:
        body.append(legend([("l1", "Discharge", "line"), ("lm", "Baseflow (straight line)", "line"),
                            ("fsoft", "Event runoff", "box")], L, foot + 42))
    return svg(W, height, "".join(body), "Storm timeline: basin precipitation and discharge")


def scatter_chart(rows, summary):
    L, R, top, B = 64, 16, 40, 44
    size = 440
    pts = [(num(r["obs_mm"]), num(r["prism_mm"]), r) for r in rows]
    hi = max(max(o, p) for o, p, _ in pts)
    tk, _, vmax = ticks(0, hi, 5)
    x = scale(0, vmax, L, L + size)
    y = scale(0, vmax, top + size, top)
    body = []
    for v in tk:
        body.append(f'<line x1="{x(v):.1f}" y1="{top}" x2="{x(v):.1f}" y2="{top + size}" class="grid"/>'
                    f'<line x1="{L}" y1="{y(v):.1f}" x2="{L + size}" y2="{y(v):.1f}" class="grid"/>'
                    f'<text x="{x(v):.1f}" y="{top + size + 16}" class="tm" text-anchor="middle">{v:g}</text>'
                    f'<text x="{L - 6}" y="{y(v) + 4:.1f}" class="tm" text-anchor="end">{v:g}</text>')
    body.append(f'<line x1="{x(0)}" y1="{y(0)}" x2="{x(vmax)}" y2="{y(vmax)}" class="lm"/>'
                f'<text x="{x(vmax * 0.9) + 6:.1f}" y="{y(vmax * 0.9) + 4:.1f}" class="tm">1:1</text>')
    for o, p, r in sorted(pts, key=lambda t: t[2]["in_sample"] == "True"):  # independent gauges drawn on top
        cls = "f1" if r["in_sample"] == "True" else "f2"
        tip = (f"{r['station'].title()} ({r['network']}): gauge {o:.0f} mm, PRISM {p:.0f} mm"
               + (" (PRISM input)" if r["in_sample"] == "True" else " (independent)"))
        body.append(f'<circle cx="{x(o):.1f}" cy="{y(p):.1f}" r="4" class="{cls} ring"/>'
                    f'<circle cx="{x(o):.1f}" cy="{y(p):.1f}" r="12" class="hit" data-tip="{esc(tip)}"/>')
    body.append(f'<text x="{L + size / 2}" y="{top + size + 34}" class="t" text-anchor="middle">Gauge storm total (mm)</text>'
                f'<text transform="translate(16 {top + size / 2}) rotate(-90)" class="t" text-anchor="middle">'
                'PRISM at the gauge (mm)</text>')
    body.append(legend([("f1", "PRISM input gauges", "box"), ("f2", "Independent gauges", "box")], L, 20))
    # Statistics beside the plot.
    sx = L + size + 24
    lines = []
    for key, label in (("all", "All gauges"), ("in_sample", "PRISM inputs"), ("independent", "Independent")):
        s = (summary or {}).get(key) or {}
        if s.get("n"):
            lines.append((label, f"n={s['n']}, PRISM {s['pbias_pct']:+.1f}%", f"r {s['r']:.2f}, RMSE {s['rmse_mm']:.1f} mm"
                          if s.get("r") is not None else f"RMSE {s['rmse_mm']:.1f} mm"))
    for i, (a, b, c) in enumerate(lines):
        yy = top + 16 + i * 52
        body.append(f'<text x="{sx}" y="{yy}" class="th">{esc(a)}</text><text x="{sx}" y="{yy + 16}" class="t">'
                    f'{esc(b)}</text><text x="{sx}" y="{yy + 32}" class="tm">{esc(c)}</text>')
    return svg(W, top + size + B, "".join(body), "PRISM versus rain gauges, storm totals")


def runoff_chart(rows):
    rows = sorted(rows, key=lambda r: -(num(r["area_km2"]) or 0))
    has_rain = any(num(r.get("rain_ratio_low")) is not None for r in rows)
    L, R, top, rh = 300, 24, 44, 26
    hi = max([num(r["ratio_high"]) for r in rows] + [num(r.get("rain_ratio_high")) or 0 for r in rows] + [1.0])
    tk, _, xmax = ticks(0, hi, 6)
    x = scale(0, xmax, L, W - R)
    height = top + rh * len(rows) + 46
    body = []
    cls_of = {"unregulated": ("f1", "o1", "l1"), "minor": ("f2", "o2", "l2"), "regulated": ("f3", "o3", "l3")}
    for v in tk:
        body.append(f'<line x1="{x(v):.1f}" y1="{top - 6}" x2="{x(v):.1f}" y2="{height - 40}" class="grid"/>'
                    f'<text x="{x(v):.1f}" y="{height - 26}" class="tm" text-anchor="middle">{v:g}</text>')
    body.append(f'<line x1="{x(1):.1f}" y1="{top - 6}" x2="{x(1):.1f}" y2="{height - 40}" class="axis"/>'
                f'<text x="{x(1) + 4:.1f}" y="{top - 10}" class="tm">1 = all of it ran off</text>')
    for i, r in enumerate(rows):
        yc = top + i * rh + rh / 2
        fcls, ocls, lcls = cls_of.get(r["regulation"], ("f1", "o1", "l1"))
        name = r["name"].title()
        name = name if len(name) <= 34 else name[:33] + "…"
        body.append(f'<text x="{L - 10}" y="{yc + 4:.1f}" class="t" text-anchor="end">{esc(name)}</text>'
                    f'<text x="{L - 10}" y="{yc + 15:.1f}" class="tm" text-anchor="end">'
                    f'{num(r["area_km2"]):,.0f} km², {esc(r["regulation"])}</text>')
        lo, hi_ = num(r["ratio_low"]), num(r["ratio_high"])
        w = max(x(hi_) - x(lo), 4)
        body.append(f'<rect x="{x(lo):.1f}" y="{yc - 6:.1f}" width="{w:.1f}" height="8" rx="4" class="{fcls}"/>')
        tip = f"{r['name'].title()}: runoff ratio {lo:.2f}–{hi_:.2f}"
        rl, rr = num(r.get("rain_ratio_low")), num(r.get("rain_ratio_high"))
        if rl is not None:
            body.append(f'<line x1="{x(rl):.1f}" y1="{yc + 6:.1f}" x2="{x(rr):.1f}" y2="{yc + 6:.1f}" class="{lcls}"/>'
                        f'<circle cx="{x(rl):.1f}" cy="{yc + 6:.1f}" r="3" class="{ocls}"/>'
                        f'<circle cx="{x(rr):.1f}" cy="{yc + 6:.1f}" r="3" class="{ocls}"/>')
            tip += f"; rain only {rl:.2f}–{rr:.2f} ({num(r['snow_pct']):.0f}% snow)"
        body.append(f'<rect x="0" y="{yc - rh / 2:.1f}" width="{W}" height="{rh}" class="hit" data-tip="{esc(tip)}"/>')
    body.append(f'<text x="{(L + W - R) / 2:.1f}" y="{height - 8}" class="t" text-anchor="middle">'
                'Runoff ratio (event runoff ÷ precipitation volume; bar = low–high baseflow)</text>')
    items = [("f1", "Unregulated", "box"), ("f2", "Minor dams", "box"), ("f3", "Regulated", "box")]
    if has_rain:
        items.append(("o1", "Thin line: runoff ÷ rain only", "ring"))
    body.append(legend(items, 16, 20))
    return svg(W, height, "".join(body), "Runoff ratio by gauge")


def uncertainty_chart(unc):
    comps = unc["components"]
    L, R, top, rh = 150, 260, 30, 34
    span = max([abs(c["low_pct"]) for c in comps] + [abs(c["high_pct"]) for c in comps] + [abs(x) for x in unc["range_pct"]] + [1])
    tk, xlo, xhi = ticks(-span, span, 6)
    x = scale(xlo, xhi, L, W - R)
    rows = comps + [{"name": "total range", "assessed": True, "low_pct": unc["range_pct"][0],
                     "high_pct": unc["range_pct"][1], "detail": "components added"}]
    height = top + rh * len(rows) + 40
    body = []
    for v in tk:
        body.append(f'<line x1="{x(v):.1f}" y1="{top - 6}" x2="{x(v):.1f}" y2="{height - 34}" class="grid"/>'
                    f'<text x="{x(v):.1f}" y="{height - 20}" class="tm" text-anchor="middle">{v:+g}%</text>')
    body.append(f'<line x1="{x(0):.1f}" y1="{top - 6}" x2="{x(0):.1f}" y2="{height - 34}" class="axis"/>')
    for i, c in enumerate(rows):
        yc = top + i * rh + rh / 2
        total = c["name"] == "total range"
        body.append(f'<text x="{L - 10}" y="{yc + 4:.1f}" class="{"th" if total else "t"}" text-anchor="end">'
                    f'{esc(c["name"].capitalize())}</text>')
        if not c["assessed"] and not c["low_pct"] and not c["high_pct"]:
            body.append(f'<text x="{x(0) + 6:.1f}" y="{yc + 4:.1f}" class="tm">not assessed</text>')
        else:
            x0, x1 = x(c["low_pct"]), x(c["high_pct"])
            body.append(f'<rect x="{min(x0, x1):.1f}" y="{yc - 5:.1f}" width="{max(abs(x1 - x0), 3):.1f}" height="10" '
                        f'rx="4" class="{"f2" if total else "f1"}"/>'
                        f'<text x="{W - R + 8}" y="{yc + 4:.1f}" class="tm">{c["low_pct"]:+.1f}% to {c["high_pct"]:+.1f}%</text>')
        body.append(f'<rect x="0" y="{yc - rh / 2:.1f}" width="{W}" height="{rh}" class="hit" '
                    f'data-tip="{esc(c["name"] + ": " + c["detail"])}"/>')
    body.append(f'<text x="{(L + W - R) / 2:.1f}" y="{height - 4}" class="t" text-anchor="middle">'
                'Change from the reported volume (%)</text>')
    return svg(W, height, "".join(body), "Uncertainty components of the storm volume")


def snow_chart(rows):
    L, R, top, B, ph = 64, 16, 40, 44, 300
    pts = [(num(r["elevation_m"]), num(r["observed_snow_pct"]), num(r["prism_snow_pct"]), r) for r in rows]
    pts = [p for p in pts if p[0] is not None and p[1] is not None]
    e_tk, elo, ehi = ticks(min(p[0] for p in pts), max(p[0] for p in pts), 5)
    x = scale(elo, ehi, L, W - R)
    y = scale(0, 100, top + ph, top)
    body = []
    for v in range(0, 101, 20):
        body.append(f'<line x1="{L}" y1="{y(v):.1f}" x2="{W - R}" y2="{y(v):.1f}" class="grid"/>'
                    f'<text x="{L - 6}" y="{y(v) + 4:.1f}" class="tm" text-anchor="end">{v}%</text>')
    for v in e_tk:
        body.append(f'<text x="{x(v):.1f}" y="{top + ph + 16}" class="tm" text-anchor="middle">{v:,.0f}</text>')
    for e, o, p, r in pts:
        if p is not None:
            body.append(f'<line x1="{x(e):.1f}" y1="{y(o):.1f}" x2="{x(e):.1f}" y2="{y(p):.1f}" class="lm"/>')
    for e, o, p, r in pts:
        body.append(f'<circle cx="{x(e):.1f}" cy="{y(o):.1f}" r="4" class="f1 ring"/>')
        if p is not None:
            body.append(f'<circle cx="{x(e):.1f}" cy="{y(p):.1f}" r="4" class="f2 ring"/>')
        tip = (f"{r['name']} ({e:,.0f} m): observed {o:.0f}%" + (f", PRISM split {p:.0f}%" if p is not None else "")
               + f"; precipitation {num(r['precip_mm']):.0f} mm, SWE gain {num(r['swe_gain_mm']):.0f} mm")
        body.append(f'<rect x="{x(e) - 8:.1f}" y="{top}" width="16" height="{ph}" class="hit" data-tip="{esc(tip)}"/>')
    body.append(f'<line x1="{L}" y1="{y(0):.1f}" x2="{W - R}" y2="{y(0):.1f}" class="axis"/>')
    body.append(f'<text x="{(L + W - R) / 2:.1f}" y="{top + ph + 34}" class="t" text-anchor="middle">Station elevation (m)</text>'
                f'<text transform="translate(16 {top + ph / 2}) rotate(-90)" class="t" text-anchor="middle">'
                'Snow, % of precipitation</text>')
    body.append(legend([("f1", "Observed (snow-water gained, a lower bound)", "box"),
                        ("f2", "PRISM split", "box")], L, 20))
    return svg(W, top + ph + B, "".join(body), "Snow share at SNOTEL stations")


# ---------- page ----------

PAGE_CSS = """
:root{%s}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){%s}}
:root[data-theme="dark"]{%s}
*{box-sizing:border-box} body{margin:0;background:var(--page);color:var(--ink);
 font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:820px;margin:0 auto;padding:24px 16px 64px} h1{font-size:24px;margin:0 0 4px}
h2{font-size:18px;margin:36px 0 4px} p{margin:4px 0 12px;color:var(--ink2)} .note{color:var(--muted);font-size:13px}
.card{background:var(--surface);border-radius:12px;padding:16px;box-shadow:0 0 0 1px rgba(127,127,127,.15)}
.card svg{width:100%%;height:auto;display:block}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0}
.tile{background:var(--surface);border-radius:12px;padding:12px 14px;box-shadow:0 0 0 1px rgba(127,127,127,.15)}
.tile .k{font-size:12px;color:var(--muted)} .tile .v{font-size:26px;font-weight:600} .tile .s{font-size:13px;color:var(--ink2)}
details{margin-top:8px} summary{cursor:pointer;color:var(--ink2);font-size:13px}
.tw{overflow-x:auto} table{border-collapse:collapse;font-size:13px;margin-top:8px;font-variant-numeric:tabular-nums}
td,th{padding:4px 10px;border-bottom:1px solid var(--grid);text-align:left;white-space:nowrap}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--surface);font-size:12px;padding:6px 8px;
 border-radius:6px;max-width:320px;display:none;z-index:9}
.st-pass{color:var(--good)} .st-warn{color:var(--ink)} .st-fail{color:var(--critical)}
.st-warn::before{content:"⚠ ";color:var(--warning)} .st-pass::before{content:"✓ "} .st-fail::before{content:"✕ "}
""" + CHART_CSS

TIP_JS = """
const tip=document.getElementById('tip');
document.addEventListener('mousemove',e=>{const t=e.target.closest('[data-tip]');
 if(!t){tip.style.display='none';return}
 tip.textContent=t.getAttribute('data-tip');tip.style.display='block';
 const x=Math.min(e.clientX+14,innerWidth-tip.offsetWidth-8);tip.style.left=x+'px';tip.style.top=(e.clientY+14)+'px'});
"""


def css_vars(colors):
    return ";".join(f"--{k}:{v}" for k, v in colors.items())


def section(title, intro, chart, tbl, note=""):
    return (f"<h2>{esc(title)}</h2><p>{intro}</p><div class=\"card\">{chart}{tbl}</div>"
            + (f'<p class="note">{note}</p>' if note else ""))


def missing(title, why):
    return f'<h2>{esc(title)}</h2><p class="note">Not shown: {why}</p>'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="folder the PRISM Data Wrangler tools wrote to")
    ap.add_argument("--start", help="first PRISM day (default: the latest storm_total.py window, else the cube)")
    ap.add_argument("--end", help="last PRISM day")
    ap.add_argument("--gauges", nargs="+", help="USGS sites for the storm timeline (default: up to 3 of the largest "
                    "compared gauges not regulated by dams)")
    ap.add_argument("--res", default="800m", choices=["800m", "4km"])
    ap.add_argument("--title", help="report title (default: the basin name from the folder)")
    a = ap.parse_args()
    run = manifest.start_run("report.py", a)

    out = Path(a.out)
    nc = out / f"ppt_{a.res}_daily_clip.nc"
    if not nc.exists():
        sys.exit(f"No cube at {nc}. Run prism_watershed.py with --out {a.out} first.")
    with xr.open_dataset(nc) as ds:
        cube_days = ds.time.values.astype("datetime64[D]")
    d0, d1 = default_window(out, cube_days)
    start, end = a.start or d0, a.end or d1
    span = f"{start.replace('-', '')}_{end.replace('-', '')}"
    basin = daily_basin(out, a.res, start, end)
    vdir = out / "validation"

    title = a.title
    if not title:
        totals = out / "totals.gpkg"
        title = out.name.replace("_", " ")
        if totals.exists():
            import geopandas as gpd
            bt = gpd.read_file(totals, layer="basin_total")
            if len(bt) and bt.get("name") is not None:
                title = str(bt["name"].iloc[0]) + (f" (HUC {bt['huc'].iloc[0]})" if bt.get("huc") is not None
                                                   and bt["huc"].iloc[0] else "")
    d_start, d_end = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    when = f"PRISM days {d_start:%b} {d_start.day} – {d_end:%b} {d_end.day}, {d_end.year}"

    unc_path = out / f"ppt_{a.res}_uncertainty_{span}.json"
    unc = json.loads(unc_path.read_text()) if unc_path.exists() else None
    v = basin["volume_m3"]
    qc = latest_qc(out)
    worst = max(qc.values(), key={"skip": 0, "pass": 1, "warn": 2, "fail": 3}.get) if qc else None
    rel = sorted(set(basin["releases"]))
    tiles = [("Storm volume", f"{v / 1e9:.3f} km³",
              f"range {unc['range_m3'][0] / 1e9:.2f}–{unc['range_m3'][1] / 1e9:.2f} km³" if unc else
              f"{v / M3_PER_ACRE_FT:,.0f} acre-ft"),
             ("Basin mean", f"{basin['mean_mm']:.1f} mm", f"max {basin['max_mm']:.0f} mm in one cell"),
             ("PRISM releases", ", ".join(f"r{r}" for r in rel if r) or "unknown",
              "final" if rel and min(rel) >= 8 else "not final: revised for ~6 months")]
    norm_path = out / f"ppt_{a.res}_normals_{span}.json"
    if norm_path.exists():
        nj = json.loads(norm_path.read_text())
        if nj.get("percent_of_normal") is not None:
            months = "; ".join(f"{m['storm_pct_of_month']:.0f}% of a normal {m['month']}" for m in nj["months"]
                               if m.get("storm_pct_of_month") is not None)
            tiles.append(("Vs. 1991–2020 normal", f"{nj['percent_of_normal']:,.0f}%",
                          f"of normal for these days ({nj['window_normal_mm']:.1f} mm); {months}"))
    if basin["rain"] is not None:
        snow = sum(basin["snow"]) / max(sum(basin["ppt"]), 1e-9) * 100
        tiles.append(("Snow share", f"{snow:.1f}%", "of precipitation (0–3 °C ramp)"))
    if worst:
        tiles.append(("QC", f'<span class="st-{worst}">{worst.upper()}</span>',
                      "; ".join(f"{k.replace('.py', '')} {v_}" for k, v_ in qc.items())))
    tiles_html = "".join(f'<div class="tile"><div class="k">{esc(k)}</div><div class="v">{v_}</div>'
                         f'<div class="s">{esc(s)}</div></div>' for k, v_, s in tiles)

    sections, figures = [], {}
    # 1. Timeline.
    runoff = [r for r in read_csv(vdir / "runoff_summary.csv")
              if r.get("prism_start") == start and r.get("prism_end") == end]
    gauges = pick_gauges(runoff, a.gauges)
    flows = []
    for g in gauges:
        rows = read_csv(vdir / f"runoff_{g['site']}_flow.csv")
        flows.append([(dt.datetime.fromisoformat(r["time_utc"]), float(r["q_cfs"]), float(r["baseflow_line_cfs"]))
                      for r in rows])
    figures["timeline"] = timeline_chart(basin, gauges, flows)
    trows = [[d, f"{p:.1f}"] + ([f"{r_:.1f}", f"{s_:.1f}"] if basin["rain"] is not None else [])
             for d, p, r_, s_ in zip(basin["days"], basin["ppt"], basin["rain"] or basin["ppt"], basin["snow"] or basin["ppt"])]
    note = ("" if gauges else "No gauge comparison for this window yet (run validate_runoff.py with the same dates); "
            "showing precipitation only.")
    if any(g.get("flow_complete") == "False" for g in gauges):
        note += " The flow record ends before the requested recession window; rerun validate_runoff.py later."
    sections.append(section("Storm timeline",
                            "When the precipitation fell over the basin, and how the rivers answered. The shaded area "
                            "above the baseflow line is the event runoff used in the runoff ratio.",
                            figures["timeline"],
                            table(["PRISM day", "Precipitation (mm)"] + (["Rain (mm)", "Snow (mm)"] if basin["rain"]
                                                                         is not None else []), trows), note))
    # 2. PRISM vs gauges.
    sj = vdir / "stations_summary.json"
    st_sum = json.loads(sj.read_text()) if sj.exists() else None
    if st_sum and st_sum.get("window", {}).get("start") == start and st_sum["window"].get("end") == end:
        st = [r for r in read_csv(vdir / "stations.csv") if r["status"] == "ok" and num(r["obs_mm"]) is not None
              and num(r["prism_mm"]) is not None]
        figures["gauges"] = scatter_chart(st, st_sum.get("event_totals"))
        sections.append(section(
            "PRISM vs rain gauges",
            "Storm totals at each gauge in the basin and a 10 km buffer. Most gauges are PRISM inputs, so their "
            "agreement is optimistic; independent gauges are the fairer test.", figures["gauges"],
            table(["Gauge", "Network", "Gauge (mm)", "PRISM (mm)", "PRISM input"],
                  [[r["station"].title(), r["network"], r["obs_mm"], r["prism_mm"], r["in_sample"]] for r in st])))
    else:
        sections.append(missing("PRISM vs rain gauges", "run validate_stations.py for this window."))
    # 3. Runoff.
    ok = [r for r in runoff if r["status"] == "ok"]
    if ok:
        figures["runoff"] = runoff_chart(ok)
        sections.append(section(
            "Where the water went",
            "Runoff ratio at each compared USGS gauge, largest basin first. Dams hold water back (regulated gauges "
            "near 0); snow that stays on the ground lowers the all-precipitation ratio, so the rain-only ratio "
            "brackets it from above.", figures["runoff"],
            table(["Gauge", "Area (km²)", "Regulation", "Precip (mm)", "Ratio", "Rain-only ratio", "Snow %"],
                  [[r["name"].title(), r["area_km2"], r["regulation"], r["precip_mm"],
                    f"{r['ratio_low']}–{r['ratio_high']}",
                    f"{r.get('rain_ratio_low') or ''}–{r.get('rain_ratio_high') or ''}", r.get("snow_pct", "")]
                   for r in sorted(ok, key=lambda r: -(num(r["area_km2"]) or 0))]),
            "Flow recorded to " + ", ".join(sorted({r["flow_end_utc"][:16] for r in ok})) + " UTC."))
    else:
        sections.append(missing("Where the water went", "run validate_runoff.py for this window."))
    # 4. Uncertainty.
    if unc:
        figures["uncertainty"] = uncertainty_chart(unc)
        sections.append(section(
            "Volume and its uncertainty",
            f"{v / 1e9:.3f} km³ ({v / M3_PER_ACRE_FT:,.0f} acre-ft) fell on the basin. Each bar is how far one "
            "source of error could move it; the total adds them. It is a bound on what has been measured, not a "
            "confidence interval (docs/UNCERTAINTY.md).", figures["uncertainty"],
            table(["Component", "Low", "High", "Detail"],
                  [[c["name"], f"{c['low_pct']:+.2f}%", f"{c['high_pct']:+.2f}%", c["detail"]]
                   for c in unc["components"]])))
    else:
        sections.append(missing("Volume and its uncertainty", "run storm_total.py for this window."))
    # 5. Snow.
    sn_sum = vdir / "snotel_summary.json"
    sn = json.loads(sn_sum.read_text()) if sn_sum.exists() else None
    if sn and sn.get("window", {}).get("start") == start and sn["window"].get("end") == end:
        rows = [r for r in read_csv(vdir / "snotel.csv") if r["status"] == "ok"]
        if rows:
            figures["snow"] = snow_chart(rows)
            ratio = sn.get("prism_over_observed_snow")
            sections.append(section(
                "Snow check at SNOTEL stations",
                "Snow as a share of each station's precipitation: observed (snow-water the pillow gained, a lower "
                "bound since some melts) and PRISM's rain/snow split at the station's cell."
                + (f" Over {sn['stations']} stations PRISM's snow is {ratio}× the observed gain." if ratio else ""),
                figures["snow"],
                table(["Station", "Elevation (m)", "Precip (mm)", "SWE gain (mm)", "Observed %", "PRISM split %"],
                      [[r["name"], r["elevation_m"], r["precip_mm"], r["swe_gain_mm"], r["observed_snow_pct"],
                        r["prism_snow_pct"]] for r in rows])))
    else:
        sections.append(missing("Snow check at SNOTEL stations", "run validate_snow.py for this window."))

    rdir = out / "report"
    fdir = rdir / "figures"
    fdir.mkdir(parents=True, exist_ok=True)
    fig_paths = []
    for name, s in figures.items():
        p = fdir / f"{name}_{span}.svg"
        p.write_text(standalone(s))
        fig_paths.append(p)
    made = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)} storm report</title><style>'
            + PAGE_CSS % (css_vars(LIGHT), css_vars(DARK), css_vars(DARK))
            + f'</style></head><body><main><h1>{esc(title)}</h1><p>{esc(when)}. Each PRISM day is the 24 hours '
            f'ending 12:00 UTC on its date.</p><div class="tiles">{tiles_html}</div>{"".join(sections)}'
            f'<p class="note" style="margin-top:40px">Made {esc(made)} by PRISM Data Wrangler report.py from '
            f'{esc(out.resolve())}. PRISM data: PRISM Group, Oregon State University. Gauges: RCC-ACIS, USGS, '
            f'NRCS. Figures as SVG in report/figures/.</p></main><div id="tip"></div><script>{TIP_JS}</script>'
            f'</body></html>')
    html_path = rdir / f"storm_report_{span}.html"
    html_path.write_text(page)
    run.update(window={"start": start, "end": end}, outputs=manifest.files_entry([html_path] + fig_paths, out))
    manifest.append_run(out, run)
    print(f"Report: {html_path}")
    print(f"Figures: {', '.join(p.name for p in fig_paths)}")


if __name__ == "__main__":
    main()
