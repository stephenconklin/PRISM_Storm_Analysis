#!/usr/bin/env python3
"""
Check that every external service PRISM Storm Analysis uses answers the way the code expects.

One light request per service, none of which downloads data or counts toward a download limit:

    prism_releases  PRISM releaseDate service (release numbers without downloading)
    prism_grids     PRISM gridCount service (same host as the daily grid downloads)
    prism_normals   PRISM 1991-2020 normals (HEAD request only)
    wbd             USGS Watershed Boundary Dataset (count query)
    usgs            USGS Water Data API, monitoring-locations (1 request of the 1,000/hour allowed)
    nldi            USGS NLDI (one gauge's feature, not its basin)
    nid             National Inventory of Dams (count query)
    acis            RCC-ACIS MultiStnData (one day, a small box)
    awdb            NRCS AWDB (SNOTEL station metadata)

The probes use fixed example sites (a San Juan gauge and box); they test the services, not a basin.
Endpoint changes break this project more than anything else: run this when a script fails with a
network error, and before relying on a service after a long gap.

Usage examples:
    python check_endpoints.py
    python check_endpoints.py --only prism_releases usgs
"""
import argparse
import datetime as dt
import json
import sys
import time

import requests

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from observations import ACIS_URL, AWDB_URL, HEADERS, NID_URL, WATERDATA_URL, WBD_URL
from prism_watershed import RELEASE_URL
from storm_context import NORMALS_URL, normal_name

PROBE_SITE = "09342500"                      # San Juan River at Pagosa Springs (any active gauge would do)
PROBE_BBOX = (-107.10, 37.20, -106.95, 37.30)  # a small box near it


def _day():
    return dt.date.today() - dt.timedelta(days=10)


def probe_prism_releases():
    d = _day().strftime("%Y%m%d")
    r = requests.get(RELEASE_URL.format(region="us", res="4km", var="ppt", start=d, end=d), params={"json": "true"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    rows = r.json()
    assert isinstance(rows, list) and rows and int(rows[0][3]) >= 1, f"unexpected reply: {str(rows)[:120]}"
    return f"release {rows[0][3]} for {rows[0][0]}"


def probe_prism_grids():
    d = _day().strftime("%Y%m%d")
    r = requests.get(f"https://services.nacse.org/prism/data/get/gridCount/us/ppt/{d}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    assert r.text.strip().isdigit(), f"unexpected reply: {r.text[:120]}"
    return f"grid count {r.text.strip()}"


def probe_prism_normals():
    url = NORMALS_URL.format(region="us", res="4km", var="ppt", step="monthly", name=normal_name("ppt", "4km", "us", 1))
    r = requests.head(url, headers=HEADERS, timeout=30, allow_redirects=True)
    r.raise_for_status()
    return f"{int(r.headers.get('Content-Length', 0)) / 1e6:.1f} MB zip"


def probe_wbd():
    r = requests.get(WBD_URL.format(layer=2), params={"where": "huc4='1408'", "returnCountOnly": "true", "f": "json"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    assert r.json().get("count") == 1, f"unexpected reply: {r.text[:120]}"
    return "HUC4 query answered"


def probe_usgs():
    r = requests.get(WATERDATA_URL.format(collection="monitoring-locations").replace("/items", f"/items/USGS-{PROBE_SITE}"),
                     params={"f": "json"}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    name = (r.json().get("properties") or {}).get("monitoring_location_name")
    assert name, f"unexpected reply: {r.text[:120]}"
    left = r.headers.get("X-RateLimit-Remaining")
    return name + (f"; {left} requests left this hour" if left else "")


def probe_nldi():
    r = requests.get(f"https://api.water.usgs.gov/nldi/linked-data/nwissite/USGS-{PROBE_SITE}", headers=HEADERS,
                     timeout=30)
    r.raise_for_status()
    assert r.json().get("features"), f"unexpected reply: {r.text[:120]}"
    return "gauge feature returned"


def probe_nid():
    w, s, e, n = PROBE_BBOX
    r = requests.get(NID_URL, params={"where": "1=1", "geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope",
                                      "inSR": 4326, "returnCountOnly": "true", "f": "json"}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    assert "count" in r.json(), f"unexpected reply: {r.text[:120]}"
    return f"{r.json()['count']} dams in the probe box"


def probe_acis():
    d = _day().isoformat()
    body = {"bbox": ",".join(map(str, PROBE_BBOX)), "sdate": d, "edate": d,
            "elems": [{"name": "pcpn", "interval": "dly"}], "meta": ["name"]}
    r = requests.post(ACIS_URL, json=body, headers=HEADERS, timeout=30)
    r.raise_for_status()
    assert "data" in r.json(), f"unexpected reply: {r.text[:120]}"
    return f"{len(r.json()['data'])} stations in the probe box"


def probe_awdb():
    r = requests.get(f"{AWDB_URL}/stations", params={"stationTriplets": "*:CO:SNTL", "returnStationElements": "false",
                                                     "activeOnly": "true"}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    assert isinstance(r.json(), list) and r.json(), f"unexpected reply: {r.text[:120]}"
    return f"{len(r.json())} Colorado SNOTEL stations"


PROBES = {name[len("probe_"):]: fn for name, fn in globals().items() if name.startswith("probe_")}
# Which services each pipeline step needs.
STEP_SERVICES = {"ppt": ["prism_releases", "prism_grids"], "tmean": ["prism_releases", "prism_grids"],
                 "stations": ["acis"], "subbasins": ["wbd"], "normals": ["prism_normals"],
                 "runoff": ["usgs", "nldi", "nid"], "snow": ["awdb"]}


def check(names=None):
    """[{name, ok, detail, seconds}] for the named services (default all)."""
    results = []
    for name in names or PROBES:
        t0 = time.monotonic()
        try:
            detail, ok = PROBES[name](), True
        except (requests.RequestException, AssertionError, ValueError, KeyError, IndexError) as e:
            detail, ok = f"{e.__class__.__name__}: {e}"[:300], False
        results.append({"name": name, "ok": ok, "detail": detail, "seconds": round(time.monotonic() - t0, 2)})
    return results


def report(results):
    return [f"  {'ok  ' if r['ok'] else 'FAIL'}  {r['name']:<15} {r['seconds']:5.1f} s  {r['detail']}" for r in results]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="+", choices=sorted(PROBES), help="check only these services")
    ap.add_argument("--json", action="store_true", help="print the results as JSON")
    a = ap.parse_args()
    results = check(a.only)
    print(json.dumps(results, indent=2) if a.json else "\n".join(report(results)))
    if not all(r["ok"] for r in results):
        sys.exit(4)


if __name__ == "__main__":
    main()
