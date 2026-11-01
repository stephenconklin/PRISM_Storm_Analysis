#!/usr/bin/env python3
"""
Look up USGS Watershed Boundary Dataset (WBD) hydrologic unit codes by name.

Searches the WBD REST service (case-insensitive, partial match) and prints each
matching HUC with its level, name, states and area. Pass a code from the
results to prism_watershed.py --huc.

Levels: 2 region, 4 subregion, 6 basin, 8 subbasin, 10 watershed, 12 subwatershed.

Usage examples:
    python find_huc.py animas                      # HUC4 through HUC10 (default levels)
    python find_huc.py "san juan" --digits 4 8     # only HUC4 and HUC8
    python find_huc.py "rio grande" --state NM     # only units touching New Mexico
"""
import argparse
import sys

import requests

WBD_URL = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/{layer}/query"
LEVELS = {2: "region", 4: "subregion", 6: "basin", 8: "subbasin", 10: "watershed",
          12: "subwatershed", 14: "HUC14", 16: "HUC16"}


def build_where(name, state=None):
    """SQL where clause for a case-insensitive partial name match (and optional state)."""
    where = f"UPPER(name) LIKE '%{name.upper().replace(chr(39), chr(39) * 2)}%'"
    if state:
        where += f" AND states LIKE '%{state.upper().replace(chr(39), '')}%'"
    return where


def search(name, digits, state=None, limit=50):
    """Return [(huc, digits, name, states, area_km2), ...] from the WBD service."""
    field = f"huc{digits}"
    params = {"where": build_where(name, state), "outFields": f"{field},name,states,areasqkm",
              "returnGeometry": "false", "orderByFields": field,
              "resultRecordCount": limit, "f": "json"}
    r = requests.get(WBD_URL.format(layer=digits // 2), params=params, timeout=60)
    r.raise_for_status()
    js = r.json()
    if "error" in js:
        sys.exit(f"WBD query failed for HUC{digits}: {js['error'].get('message')}")
    return [(f["attributes"][field], digits, f["attributes"]["name"], f["attributes"]["states"] or "",
             f["attributes"]["areasqkm"] or 0) for f in js.get("features", [])]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="part of the watershed name, e.g. animas or 'san juan'")
    ap.add_argument("--digits", type=int, nargs="+", default=[4, 6, 8, 10], choices=sorted(LEVELS),
                    help="HUC levels to search (default 4 6 8 10)")
    ap.add_argument("--state", help="two-letter state code the unit must touch, e.g. CO")
    ap.add_argument("--limit", type=int, default=50, help="max results per level (default 50)")
    a = ap.parse_args()

    rows = []
    for d in a.digits:
        found = search(a.name, d, a.state, a.limit)
        if len(found) == a.limit:
            print(f"(HUC{d}: showing the first {a.limit}; narrow the name or use --state)", file=sys.stderr)
        rows += found
    if not rows:
        sys.exit(f"No WBD units named like '{a.name}' at levels {a.digits}.")

    print(f"{'HUC':<16} {'Level':<13} {'Area km²':>10}  {'States':<12} Name")
    for huc, d, name, states, area in rows:
        print(f"{huc:<16} {LEVELS[d]:<13} {area:>10,.0f}  {states:<12} {name}")


if __name__ == "__main__":
    main()
