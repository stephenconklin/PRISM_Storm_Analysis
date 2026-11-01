"""
Quality-control checks for PRISM runs (pure functions, no network or file access).

Each check returns {"check", "level", "detail", ...values}, with level one of
pass, warn, fail or skip (not enough information to run the check).
prism_watershed.py and storm_total.py run them, print a QC block with `report`
and save the results in run_manifest.json. --qc-strict turns warnings into failures.
"""
LEVEL_ORDER = {"skip": 0, "pass": 1, "warn": 2, "fail": 3}


def result(check, level, detail, **values):
    return {"check": check, "level": level, "detail": detail, **values}


def completeness(requested, have):
    """Every requested day was downloaded, clipped and is in the cube."""
    missing = [str(d) for d in requested if d not in set(have)]
    if missing:
        return result("completeness", "fail", f"{len(missing)} of {len(requested)} days missing: "
                      + ", ".join(missing), missing=missing)
    return result("completeness", "pass", f"all {len(requested)} days present")


def download_integrity(downloads):
    """Every day's zip opened and has a checksum. downloads: [{date, sha256, error}] (error '' if none);
    PRISM's own message is passed through verbatim."""
    bad = [d for d in downloads if d.get("error") or not d.get("sha256")]
    if bad:
        return result("download_integrity", "fail", "; ".join(f"{d['date']}: {d.get('error') or 'no checksum'}"
                                                              for d in bad), failed=[str(d["date"]) for d in bad])
    return result("download_integrity", "pass", f"{len(downloads)} zips open; SHA-256 recorded")


def grid_consistency(grids):
    """Same transform, shape and CRS for every day. grids: [(date, transform, shape, crs_wkt)]."""
    if not grids:
        return result("grid_consistency", "skip", "no grids")
    _, t0, s0, c0 = grids[0]
    odd = [str(d) for d, t, s, c in grids[1:] if t != t0 or tuple(s) != tuple(s0) or c != c0]
    if odd:
        return result("grid_consistency", "fail", f"different grid from {grids[0][0]}: " + ", ".join(odd))
    return result("grid_consistency", "pass", f"{len(grids)} days on one grid ({s0[0]} x {s0[1]} cells)")


def nodata_inside(missing_pct, constant_pct=0.0, warn_constant_pct=0.5, fail_pct=1.0):
    """Basin cells without data.

    missing_pct: {date: % of basin area with nodata that day beyond the constant gap};
    constant_pct: % of the basin with no data on every day (outside PRISM coverage, e.g. Canada).
    A constant gap is a warning; cells missing on only some days are a warning above 0 and a failure
    above fail_pct.
    """
    worst_day = max(missing_pct, key=missing_pct.get) if missing_pct else None
    worst = missing_pct[worst_day] if worst_day else 0.0
    notes = []
    level = "pass"
    if worst > 0:
        level = "fail" if worst > fail_pct else "warn"
        notes.append(f"{worst:.2f}% of the basin missing on {worst_day} only")
    if constant_pct > warn_constant_pct:
        level = "warn" if level == "pass" else level
        notes.append(f"{constant_pct:.1f}% of the basin outside PRISM coverage on every day")
    return result("nodata_inside", level, "; ".join(notes) or "no nodata inside the basin",
                  worst_day_pct=round(worst, 3), constant_pct=round(constant_pct, 3))


def physical_limits(daily_min, daily_max, max_mm=400.0):
    """Precipitation: no negative values (fail); a daily cell above max_mm is flagged (warn).
    daily_min, daily_max: {date: mm}."""
    neg = [str(d) for d, v in daily_min.items() if v < 0]
    if neg:
        return result("physical_limits", "fail", "negative precipitation on " + ", ".join(neg))
    high = {str(d): round(v, 1) for d, v in daily_max.items() if v > max_mm}
    if high:
        return result("physical_limits", "warn", f"daily cell maximum above {max_mm:g} mm: "
                      + ", ".join(f"{d} {v} mm" for d, v in high.items()))
    top = max(daily_max.values()) if daily_max else 0.0
    return result("physical_limits", "pass", f"no negatives; largest daily cell {top:.1f} mm (limit {max_mm:g})")


def totals_reconcile(pairs, rtol=1e-4):
    """Pairs of numbers that must agree: [(label, a, b)]."""
    if not pairs:
        return result("totals_reconcile", "skip", "nothing to compare")
    off = [(label, a, b) for label, a, b in pairs if abs(a - b) > rtol * max(abs(a), abs(b), 1e-12)]
    if off:
        return result("totals_reconcile", "fail", "; ".join(f"{label}: {a:.6g} vs {b:.6g}" for label, a, b in off))
    return result("totals_reconcile", "pass", "; ".join(label for label, _, _ in pairs) + " agree")


def release_mix(releases):
    """Warn when the window includes release 1-2 days (revised soon) or mixes releases.
    releases: {date: release number, 0 or '' if unknown}."""
    rel = {str(d): int(r) if str(r).isdigit() else 0 for d, r in releases.items()}
    if not rel:
        return result("release_mix", "skip", "no releases")
    notes = []
    early = [d for d, r in rel.items() if 0 < r <= 2]
    if early:
        notes.append(f"{len(early)} days at release 1-2 (PRISM revises them within weeks)")
    if 0 in rel.values():
        notes.append("release unknown for " + ", ".join(d for d, r in rel.items() if r == 0))
    if len(set(rel.values())) > 1:
        notes.append("mixed releases " + ", ".join(f"r{r}" for r in sorted(set(rel.values())) if r))
    final = all(r == 8 for r in rel.values())
    if notes:
        return result("release_mix", "warn", "; ".join(notes), releases=rel)
    return result("release_mix", "pass", "all days final (release 8)" if final
                  else f"all days at release {next(iter(rel.values()))}", releases=rel)


def station_density(counts, area_km2, buffer_km, min_per_1000km2=0.5):
    """PRISM input stations in the basin plus a buffer, per 1,000 km² of basin, on the sparsest day.
    counts: {date: stations} from each day's stn.csv."""
    if not counts:
        return result("station_density", "skip", "no stn.csv in the zips")
    day = min(counts, key=counts.get)
    density = counts[day] / area_km2 * 1000 if area_km2 else 0.0
    level = "warn" if density < min_per_1000km2 else "pass"
    return result("station_density", level, f"{counts[day]} stations in the basin or within {buffer_km:g} km of it on {day} "
                  f"({density:.2f} per 1,000 km²; warn below {min_per_1000km2:g})",
                  min_stations=counts[day], per_1000km2=round(density, 3))


def boundary_agreement(grid_km2, polygon_km2, max_pct=1.0):
    """Area of the cells counted as the basin (by center) vs the polygon's ellipsoidal area."""
    if not polygon_km2:
        return result("boundary_agreement", "skip", "no polygon area")
    diff = (grid_km2 - polygon_km2) / polygon_km2 * 100
    level = "warn" if abs(diff) > max_pct else "pass"
    return result("boundary_agreement", level, f"grid {grid_km2:,.1f} km² vs polygon {polygon_km2:,.1f} km² "
                  f"({diff:+.2f}%; warn beyond {max_pct:g}%)", diff_pct=round(diff, 3))


def apply_strict(results):
    """--qc-strict: warnings become failures."""
    return [{**r, "level": "fail"} if r["level"] == "warn" else r for r in results]


def worst_level(results):
    return max((r["level"] for r in results), key=LEVEL_ORDER.get, default="skip")


def report(results):
    """Lines for the QC block printed at the end of a run."""
    lines = [f"QC: {worst_level(results).upper()}"]
    for r in results:
        lines.append(f"  {r['level'].upper():<4}  {r['check']:<20} {r['detail']}")
    return lines
