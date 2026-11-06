"""
Uncertainty of a storm's precipitation volume: separate components and a bounding range
(pure functions, no network or file access). Method: docs/UNCERTAINTY.md.

Each component is {"name", "assessed", "low_pct", "high_pct", "detail"}: how far below and above
the reported volume that source of error could plausibly put it, in percent of the volume. The
overall range adds the lows and the highs (a deliberately wide bound: PRISM's errors are spatially
correlated and the components overlap, so no statistical combination is attempted).
"""


def component(name, low_pct, high_pct, detail, assessed=True, **values):
    return {"name": name, "assessed": assessed, "low_pct": round(min(low_pct, 0.0), 3),
            "high_pct": round(max(high_pct, 0.0), 3), "detail": detail, **values}


def not_assessed(name, detail):
    return {"name": name, "assessed": False, "low_pct": 0.0, "high_pct": 0.0, "detail": detail}


def gauge_component(event_totals, min_independent=5):
    """Gauge comparison (validate_stations.py event totals) scaled to the basin.

    A gauge-based volume is the PRISM volume divided by (1 + PRISM's percent bias): if PRISM is 4%
    below the gauges, gauges imply 4.2% more water. The range spans the volumes implied by all gauges
    and by independent gauges (not PRISM inputs) when there are at least min_independent of them.
    """
    if not event_totals or not event_totals.get("all", {}).get("n"):
        return not_assessed("gauges", "no station comparison for this window (run validate_stations.py)")
    implied = {}
    for key in ("all", "independent"):
        s = event_totals.get(key) or {}
        if s.get("n") and s.get("pbias_pct") is not None and (key == "all" or s["n"] >= min_independent):
            implied[key] = (1 / (1 + s["pbias_pct"] / 100) - 1) * 100
    parts = [f"{k} gauges (n={event_totals[k]['n']}) PRISM {event_totals[k]['pbias_pct']:+.1f}% "
             f"-> volume {v:+.1f}%" for k, v in implied.items()]
    ind = event_totals.get("independent") or {}
    if "independent" not in implied and ind.get("n"):
        parts.append(f"only {ind['n']} independent gauges (not used)")
    vals = list(implied.values())
    return component("gauges", min(vals), max(vals), "; ".join(parts) + ". Mostly PRISM inputs (in-sample), "
                     "and gauges catch less than falls, so this understates PRISM's error", implied_pct=implied)


def boundary_component(volumes, reported_m3):
    """Basin edge (storm_total.boundary_volumes): cell centers vs area-weighted cells. The 'touched' volume
    covers more than the basin, so it is shown but not used."""
    if not volumes:
        return not_assessed("boundary", "national grids or boundary not available")
    if not reported_m3:
        return not_assessed("boundary", "no precipitation in the window")
    w = (volumes["weighted"] / reported_m3 - 1) * 100
    t = (volumes["touched"] / reported_m3 - 1) * 100
    return component("boundary", w, w, f"area-weighted {w:+.2f}% (any cell touched {t:+.2f}%, not used: it "
                     "counts land outside the basin)")


def revision_component(rc, releases):
    """PRISM revisions so far (storm_total.revision_change), applied in both directions since later
    releases can move either way; flagged while any day is below release 8."""
    early = [r for r in releases if r < 8]
    rel = (f"release {min(early)}" if early and min(early) == max(early)
           else f"releases {min(early)}-{max(early)}" if early else "")
    note = (f"; {len(early)} of {len(releases)} days not final ({rel}), so later releases may move it more"
            if early else "; all days final")
    if not rc:
        return not_assessed("revisions", "no release history for every day" + note)
    c = abs(rc["change_pct"])
    if not rc["revised"]:
        return component("revisions", 0, 0, "no revisions recorded yet" + note, assessed=bool(not early))
    return component("revisions", -c, c, f"window volume changed {rc['change_pct']:+.2f}% from first recorded to "
                     f"latest releases" + note)


def product_component():
    return not_assessed("products", "comparison with other precipitation products not built yet")


def bounding_range(components):
    """(low %, high %) of the reported volume: the sum of the assessed components' lows and highs."""
    used = [c for c in components if c["assessed"] or c["low_pct"] or c["high_pct"]]
    return sum(c["low_pct"] for c in used), sum(c["high_pct"] for c in used)


def volume_range(reported_m3, components):
    lo, hi = bounding_range(components)
    return reported_m3 * (1 + lo / 100), reported_m3 * (1 + hi / 100)


def report(reported_m3, components):
    """Lines for the window report."""
    lo, hi = volume_range(reported_m3, components)
    lines = [f"Uncertainty: {reported_m3 / 1e9:.3f} km³, range {lo / 1e9:.3f}-{hi / 1e9:.3f} km³ "
             f"({bounding_range(components)[0]:+.1f}% to {bounding_range(components)[1]:+.1f}%; components added, "
             "see docs/UNCERTAINTY.md)"]
    for c in components:
        span = (f"{c['low_pct']:+.1f}% to {c['high_pct']:+.1f}%" if c["assessed"] or c["low_pct"] or c["high_pct"]
                else "not assessed")
        lines.append(f"  {c['name']:<10} {span:<16} {c['detail']}")
    return lines
