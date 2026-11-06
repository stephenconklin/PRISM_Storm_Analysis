"""
Per-folder settings: <out>/storm.toml.

`pipeline.py init` writes it; every script reads it for defaults, and flags on the command line win.
So once a folder has settings, most commands need only --out:

    python scripts/storm_total.py --out data/huc1408                  # the first event's window
    python scripts/storm_total.py --out data/huc1408 --event second   # another event
    python scripts/prism_watershed.py --out data/huc1408 --var tmean  # basin and download window

Keys (all optional except the basin and the download window):

    name = "San Juan, late October 2026"
    huc = "1408"                 # or boundary = "basin.gpkg" (relative to this file)
    res = "800m"                 # or "4km"
    region = "us"
    prism_cache = "~/prism_cache"   # shared national zip cache (or the PRISM_CACHE variable)

    [download]                   # PRISM days to download: the events padded by a day each side
    start = 2026-10-20
    end = 2026-11-02
    vars = ["ppt", "tmean"]

    [[events]]                   # analysis windows; scripts default to the first
    name = "trudy"
    start = 2026-10-21
    end = 2026-10-27
    after_days = 3               # optional: this event's flow window (overrides [validation] after_days),
                                 # e.g. to stop before the next storm's runoff

    [rain_snow]
    all_snow_c = 0.0
    all_rain_c = 3.0

    [validation]
    buffer_km = 10.0
    after_days = 7
    gauges = []                  # USGS sites for validate_runoff.py; empty = every gauge in the basin

    [subbasins]
    digits = 10

    [report]
    gauges = []                  # sites for the report's storm timeline; empty = picked automatically
"""
import argparse
import datetime as dt
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

FILE = "storm.toml"


class SettingsError(ValueError):
    pass


def path_for(out):
    return Path(out) / FILE


def load(out):
    """The folder's settings as a dict, or None if it has no storm.toml. Raises SettingsError if unreadable."""
    path = path_for(out)
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            s = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SettingsError(f"{path}: {e}") from e
    s["_path"] = path
    return s


def iso(v):
    return v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else (str(v) if v is not None else None)


def events(s):
    """[(name, start, end)] with ISO dates."""
    return [(str(e.get("name") or f"event{i + 1}"), iso(e["start"]), iso(e["end"]))
            for i, e in enumerate((s or {}).get("events", [])) if e.get("start") and e.get("end")]


def event(s, name=None):
    """(name, start, end) of the named event, else the first; None if there are none."""
    evs = events(s)
    if name:
        for e in evs:
            if e[0] == name:
                return e
        raise SettingsError(f"No event named '{name}' in {FILE} (events: {', '.join(e[0] for e in evs) or 'none'})")
    return evs[0] if evs else None


def boundary_path(s):
    """The boundary file, relative to the settings file when it isn't absolute."""
    b = (s or {}).get("boundary")
    if not b:
        return None
    p = Path(b).expanduser()
    base = Path(s["_path"]).parent if s.get("_path") else Path.cwd()
    return str(p if p.is_absolute() else (base / p).resolve())


def check(s):
    """Problems that would stop a run, as a list of messages ([] if none)."""
    problems = []
    if bool(s.get("huc")) == bool(s.get("boundary")):
        problems.append("give exactly one of huc or boundary")
    if s.get("res", "800m") not in ("800m", "4km"):
        problems.append("res must be 800m or 4km")
    d = s.get("download") or {}
    if not (d.get("start") and d.get("end")):
        problems.append("[download] needs start and end")
    elif iso(d["start"]) > iso(d["end"]):
        problems.append("[download] start is after end")
    for name, start, end in events(s):
        if start > end:
            problems.append(f"event {name}: start is after end")
        elif d.get("start") and d.get("end") and (start < iso(d["start"]) or end > iso(d["end"])):
            problems.append(f"event {name} ({start} to {end}) is outside the download window "
                            f"({iso(d['start'])} to {iso(d['end'])})")
    if s.get("boundary") and not Path(boundary_path(s)).exists():
        problems.append(f"boundary file {boundary_path(s)} not found")
    return problems


def event_for_window(s, start, end):
    """The raw event table whose window is start..end (ISO dates), or None."""
    return next((e for e in (s or {}).get("events", []) if iso(e.get("start")) == start and iso(e.get("end")) == end),
                None)


def defaults_for(s, role, event_name=None, window=None):
    """{argparse dest: value} the settings supply for a script. role 'download' (prism_watershed.py) takes
    the download window and basin; role 'event' takes an event's window and its own settings (after_days).
    window=(start, end) from the command line picks the event with those dates for its own settings."""
    rs, val = s.get("rain_snow") or {}, s.get("validation") or {}
    out = {"res": s.get("res"), "region": s.get("region"), "cache": s.get("prism_cache"),
           "all_snow_c": rs.get("all_snow_c"), "all_rain_c": rs.get("all_rain_c"),
           "buffer_km": val.get("buffer_km"), "after_days": val.get("after_days"),
           "digits": (s.get("subbasins") or {}).get("digits"),
           "gauges": (s.get("report") or {}).get("gauges") or None}
    gauges = [str(g) for g in val.get("gauges") or []]
    out.update({"gauge": gauges} if gauges else {"all_gauges": True})
    if role == "download":
        d = s.get("download") or {}
        out.update(start=iso(d.get("start")), end=iso(d.get("end")), huc=str(s["huc"]) if s.get("huc") else None,
                   boundary=boundary_path(s))
    elif role == "event":
        ev = event(s, event_name)
        if ev:
            out.update(start=ev[1], end=ev[2])
        raw = (event_for_window(s, *window) if window and all(window) and not event_name
               else event_for_window(s, ev[1], ev[2]) if ev else None)
        if raw and raw.get("after_days") is not None:
            out["after_days"] = raw["after_days"]
    return {k: v for k, v in out.items() if v is not None}


def _given(ap, argv):
    """dests whose option appears on the command line."""
    given = set()
    for action in ap._actions:
        for opt in action.option_strings:
            if any(tok == opt or tok.startswith(opt + "=") for tok in argv):
                given.add(action.dest)
    return given


def parse_args(ap, argv=None, role="event", out_default=None):
    """Parse the command line with defaults from <out>/storm.toml (when there is one).

    Adds --event for role 'event'. out_default(pre-parsed args) gives the folder when --out is absent
    (prism_watershed.py's data/huc<code>). Settings never override a flag that was given, nor fill
    another member of a mutually exclusive group whose flag was given. The applied settings are stored
    on the result as _settings ({dest: value}) and _settings_file.
    """
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if role == "event" and "event" not in {a.dest for a in ap._actions}:
        ap.add_argument("--event", help=f"event name from {FILE} for the default window (default: the first)")
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    for opt in ("--out", "--event", "--huc", "--boundary", "--start", "--end"):
        pre.add_argument(opt)
    known, _ = pre.parse_known_args(argv)
    out = known.out or (out_default(known) if out_default else None)
    applied, path = {}, None
    if out:
        try:
            s = load(out)
            defaults = defaults_for(s, role, known.event, (known.start, known.end)) if s is not None else {}
        except SettingsError as e:
            import runtime
            runtime.die(str(e))
        if s is not None:
            path = s["_path"]
            given = _given(ap, argv)
            actions = {a.dest: a for a in ap._actions}
            groups = [g for g in ap._mutually_exclusive_groups]
            for dest, value in defaults.items():
                action = actions.get(dest)
                if action is None or dest in given:
                    continue
                group = next((g for g in groups if action in g._group_actions), None)
                if group and any(a.dest in given or a.dest in applied for a in group._group_actions):
                    continue
                applied[dest] = value
                action.required = False
                if group:
                    group.required = False
            ap.set_defaults(**applied)
    a = ap.parse_args(argv)
    a._settings, a._settings_file = applied, str(path) if path else None
    if applied:
        print(f"Settings from {Path(path).name}: " + ", ".join(f"{k}={v}" for k, v in applied.items()
                                                              if k not in ("all_gauges", "boundary")))
    return a


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    import json
    return json.dumps(str(v))  # a TOML basic string


def render(s):
    """storm.toml text for settings s (the keys in this module's docstring), with comments."""
    lines = ["# PRISM Storm Analysis settings for this folder (see scripts/config.py).",
             "# Every script reads these for its defaults; command-line flags override them.", ""]
    for key, note in (("name", ""), ("huc", "WBD hydrologic unit code"), ("boundary", "polygon file instead of huc"),
                      ("res", "800m or 4km"), ("region", ""), ("prism_cache", "shared national zip cache")):
        if s.get(key) not in (None, ""):
            lines.append(f"{key} = {toml_value(s[key])}" + (f"   # {note}" if note else ""))
    d = s["download"]
    lines += ["", "[download]   # PRISM days to download: the events padded by a day on each side",
              f"start = {toml_value(d['start'])}", f"end = {toml_value(d['end'])}",
              f"vars = {toml_value(d.get('vars', ['ppt', 'tmean']))}"]
    for e in s.get("events", []):
        lines += ["", "[[events]]   # analysis window (PRISM days); scripts default to the first event",
                  f"name = {toml_value(e['name'])}", f"start = {toml_value(e['start'])}", f"end = {toml_value(e['end'])}"]
        if e.get("after_days") is not None:
            lines.append(f"after_days = {toml_value(e['after_days'])}   # this event's flow window, days after end")
    sections = (("rain_snow", "all snow at or below all_snow_c, all rain at or above all_rain_c (daily mean °C)"),
                ("validation", "gauges: USGS sites for validate_runoff.py; empty = every gauge in the basin"),
                ("subbasins", "HUC level for subbasin_totals.py"),
                ("report", "gauges: sites for the report's storm timeline; empty = picked automatically"))
    for name, note in sections:
        if s.get(name):
            lines += ["", f"[{name}]   # {note}"] + [f"{k} = {toml_value(v)}" for k, v in s[name].items()]
    return "\n".join(lines) + "\n"
