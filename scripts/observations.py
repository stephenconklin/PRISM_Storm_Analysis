"""
Network access for independent observations used to validate PRISM.

Kept separate from the processing in validate_stations.py, validate_runoff.py, validate_snow.py and
subbasin_totals.py so that processing stays testable offline. Every function here makes HTTP requests.

Sources (checked October 2026):
    RCC-ACIS  https://data.rcc-acis.org/MultiStnData
        Daily precipitation for COOP, CoCoRaHS, ASOS/first-order and SNOTEL stations
        (SNOTEL appears with GHCN ids starting USS). Values in inches, with the observation
        hour in local standard time.
    USGS Water Data API  https://api.waterdata.usgs.gov/ogcapi/v0/
        monitoring-locations (site metadata), continuous (15-minute values), daily.
        Replaces the legacy NWIS Water Services.
    USGS NLDI  https://api.water.usgs.gov/nldi/linked-data/nwissite/USGS-<site>/basin
        Drainage basin upstream of a gauge. (splitCatchment=true returned errors; not used.)
    National Inventory of Dams (USACE), ArcGIS feature service
        https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/NID_v1/FeatureServer/0
        Dam locations and storage (acre-ft). NLDI's ref_dams list was checked and rejected:
        it has no storage and missed Navajo and Vallecito dams.
    NRCS AWDB REST API  https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/ (checked 1 Nov 2026, no key)
        stations (SNOTEL metadata: triplet, lat/lon, elevation in ft) and data (daily WTEQ snow-water
        equivalent, PREC accumulated precipitation, PRCP daily precipitation, TAVG; inches and degF).
        Daily values are Pacific Standard Time calendar days for every station; WTEQ and PREC are
        end-of-day readings. Lagged correlation against PRISM (0.95-0.998 at four San Juan sites) put
        AWDB day D in PRISM day D+1.

Rate limits: the USGS Water Data API allows 1,000 requests per hour per IP without a key
(HTTP 429 with Retry-After when exceeded). Set USGS_API_KEY to an api.data.gov key; it is
sent as the X-Api-Key header and gets its own 1,000-per-hour allowance (checked 31 Oct 2026). Waits up to MAX_WAIT_S are
waited out; longer ones raise RateLimited.
"""
import os
import time

import requests

try:
    # Use the OS trust store, as prism_watershed.py does.
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

ACIS_URL = "https://data.rcc-acis.org/MultiStnData"
WATERDATA_URL = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/{collection}/items"
NLDI_BASIN_URL = "https://api.water.usgs.gov/nldi/linked-data/nwissite/USGS-{site}/basin"
NID_URL = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
           "NID_v1/FeatureServer/0/query")
WBD_URL = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/{layer}/query"
AWDB_URL = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
FT_TO_M = 0.3048
HEADERS = {"User-Agent": "prism-storm-analysis"}
MAX_WAIT_S = 900  # longest Retry-After to wait out before giving up (callers may change it)


class RateLimited(RuntimeError):
    """The service asked us to wait longer than MAX_WAIT_S; .retry_after is in seconds."""

    def __init__(self, url, retry_after):
        super().__init__(f"Rate limited by {url.split('/')[2]}; retry in about {retry_after // 60 + 1} min "
                         "(setting USGS_API_KEY gives a separate allowance).")
        self.retry_after = retry_after


def retry_after_s(value, default):
    """Seconds to wait from a Retry-After header: delta-seconds or an HTTP date."""
    if value is None:
        return default
    try:
        return max(0, int(value))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            import datetime as dt
            return max(0, int((parsedate_to_datetime(value) - dt.datetime.now(dt.timezone.utc)).total_seconds()))
        except (TypeError, ValueError):
            return default


def _get(url, params=None, timeout=120):
    """GET with the USGS key (if set) and retries on 429/503."""
    headers = dict(HEADERS)
    if os.environ.get("USGS_API_KEY") and "api.waterdata.usgs.gov" in url:
        headers["X-Api-Key"] = os.environ["USGS_API_KEY"]
    for attempt in range(5):
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        if r.status_code not in (429, 503):
            r.raise_for_status()
            return r
        wait = retry_after_s(r.headers.get("Retry-After"), 5 * 2 ** attempt)
        if wait > MAX_WAIT_S:
            raise RateLimited(url, wait)
        if wait > 10:
            print(f"    {url.split('/')[2]} asked to wait {wait // 60} min {wait % 60} s; waiting", flush=True)
        time.sleep(wait)
    r.raise_for_status()
    return r


def acis_daily_precip(bbox, sdate, edate):
    """Daily precipitation for every ACIS station in bbox (west, south, east, north).

    Returns the raw ACIS station list: each item has "meta" (name, ll, elev, sids, tzo)
    and "data" (one [[value, obs_hour]] per day from sdate to edate).
    """
    body = {"bbox": ",".join(f"{v:.4f}" for v in bbox), "sdate": sdate, "edate": edate,
            "elems": [{"name": "pcpn", "interval": "dly", "add": "t"}],
            "meta": ["name", "ll", "elev", "sids", "state", "tzo"]}
    r = requests.post(ACIS_URL, json=body, headers=HEADERS, timeout=120)
    r.raise_for_status()
    js = r.json()
    if "error" in js:
        raise RuntimeError(f"ACIS error: {js['error']}")
    return js.get("data", [])


def _waterdata_items(collection, params):
    """All features from a Water Data API collection, following 'next' links."""
    url, params = WATERDATA_URL.format(collection=collection), {**params, "f": "json", "limit": 10000}
    features = []
    while url:
        js = _get(url, params).json()
        features += js.get("features", [])
        url = next((l["href"] for l in js.get("links", []) if l.get("rel") == "next"), None)
        params = None  # the next link already carries the query
    return features


def _site_info(feature):
    p = feature["properties"]
    lon, lat = feature["geometry"]["coordinates"][:2]
    return {"site": str(feature.get("id") or p.get("id") or "").removeprefix("USGS-"),
            "name": p.get("monitoring_location_name"), "lon": lon, "lat": lat,
            "drainage_area_mi2": p.get("drainage_area"), "huc": p.get("hydrologic_unit_code"),
            "time_zone": p.get("time_zone_abbreviation"), "site_type": p.get("site_type")}


def usgs_site(site):
    """Site metadata: name, lon/lat, drainage area (mi²), time zone, site type."""
    js = _get(WATERDATA_URL.format(collection="monitoring-locations") + f"/USGS-{site}", {"f": "json"}).json()
    return {**_site_info(js), "site": site}


def usgs_sites(bbox):
    """Metadata for every USGS stream site in bbox, keyed by site number (one paged request)."""
    feats = _waterdata_items("monitoring-locations", {"bbox": ",".join(f"{v:.4f}" for v in bbox),
                                                      "agency_code": "USGS", "site_type_code": "ST"})
    sites = (_site_info(f) for f in feats if f.get("geometry"))
    return {s["site"]: s for s in sites if s["site"]}


def usgs_discharge_gauges(bbox, start, end):
    """USGS site numbers in bbox with a 15-minute discharge record covering start..end (ISO dates)."""
    feats = _waterdata_items("time-series-metadata", {
        "bbox": ",".join(f"{v:.4f}" for v in bbox), "parameter_code": "00060",
        "computation_period_identifier": "Points"})
    sites = set()
    for f in feats:
        p = f["properties"]
        if (p.get("begin") or "9999") <= start and (p.get("end") or "") >= end:
            sites.add(p["monitoring_location_id"].removeprefix("USGS-"))
    return sorted(sites)


def _wbd_where(digits, prefix=None, bbox=None):
    field = f"huc{digits}"
    params = {"where": f"{field} LIKE '{prefix}%'" if prefix else "1=1"}
    if bbox is not None:
        params.update(geometry=",".join(f"{v:.4f}" for v in bbox), geometryType="esriGeometryEnvelope",
                      inSR=4326, spatialRel="esriSpatialRelIntersects")
    return params


def wbd_count(digits, prefix=None, bbox=None):
    """Number of WBD units at a HUC level under a prefix or intersecting a bounding box."""
    params = {**_wbd_where(digits, prefix, bbox), "returnCountOnly": "true", "f": "json"}
    return int(_get(WBD_URL.format(layer=digits // 2), params).json().get("count", 0))


def wbd_units(digits, prefix=None, bbox=None):
    """GeoJSON features of WBD units at a HUC level (2-16 digits) under a prefix or in a bbox (NAD83)."""
    field, features, offset = f"huc{digits}", [], 0
    while True:
        params = {**_wbd_where(digits, prefix, bbox), "outFields": f"{field},name,areasqkm,states",
                  "returnGeometry": "true", "outSR": 4269, "f": "geojson",
                  "resultOffset": offset, "resultRecordCount": 500, "orderByFields": field}
        page = _get(WBD_URL.format(layer=digits // 2), params).json().get("features", [])
        features += page
        if len(page) < 500:
            return features
        offset += len(page)


def nid_dams(bbox):
    """National Inventory of Dams entries in bbox: name, id, lon/lat, storage (acre-ft), purposes.

    Associated structures (dikes and saddle dams listed with their reservoir's storage) are
    left out so a reservoir's storage is counted once.
    """
    w, s, e, n = bbox
    params = {"where": "IS_ASSOCIATED_STRUCTURE IS NULL OR IS_ASSOCIATED_STRUCTURE <> 'Yes'",
              "geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope",
              "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "outSR": 4326, "f": "json",
              "outFields": "NIDID,NAME,RIVER_OR_STREAM,NORMAL_STORAGE,NID_STORAGE,DRAINAGE_AREA,PURPOSES,YEAR_COMPLETED",
              "resultOffset": 0}
    dams = []
    while True:
        r = requests.get(NID_URL, params=params, headers=HEADERS, timeout=120)
        r.raise_for_status()
        js = r.json()
        if "error" in js:
            raise RuntimeError(f"NID query failed: {js['error']}")
        for f in js.get("features", []):
            a, g = f["attributes"], f.get("geometry") or {}
            if "x" not in g:
                continue
            dams.append({"nidid": a["NIDID"], "name": a["NAME"], "river": a["RIVER_OR_STREAM"],
                         "normal_storage_af": a["NORMAL_STORAGE"], "nid_storage_af": a["NID_STORAGE"],
                         "drainage_area_mi2": a.get("DRAINAGE_AREA"),
                         "purposes": a["PURPOSES"], "year": a["YEAR_COMPLETED"], "lon": g["x"], "lat": g["y"]})
        if not js.get("exceededTransferLimit"):
            return dedupe_dams(dams)
        params["resultOffset"] += len(js.get("features", []))


def dedupe_dams(dams):
    """Drop repeat entries for one reservoir that NID lists as several dams with the same storage.

    Two entries are the same reservoir when their storage matches and either their base names
    match ('Narraguinnep - Dam 2' -> 'narraguinnep') or they are within 2 km of each other.
    """
    def base(name):
        n = (name or "").lower().split(" - ")[0]
        for suffix in (" main dam", " dam", " dike"):
            n = n.removesuffix(suffix)
        return n.strip()

    kept = []
    for d in dams:
        storage = d["normal_storage_af"] or d["nid_storage_af"]
        dup = storage and any(
            (k["normal_storage_af"] or k["nid_storage_af"]) == storage
            and (base(k["name"]) == base(d["name"])
                 or ((k["lon"] - d["lon"]) * 88.0) ** 2 + ((k["lat"] - d["lat"]) * 111.0) ** 2 < 4.0)
            for k in kept)
        if not dup:
            kept.append(d)
    return kept


def usgs_continuous(site, start_utc, end_utc, parameter="00060"):
    """15-minute values as [(ISO time UTC, value or None, qualifier list or None, approval)]."""
    feats = _waterdata_items("continuous", {
        "monitoring_location_id": f"USGS-{site}", "parameter_code": parameter,
        "time": f"{start_utc}/{end_utc}"})
    out = [(f["properties"]["time"], f["properties"]["value"], f["properties"].get("qualifier"),
            f["properties"].get("approval_status")) for f in feats]
    return sorted(out)


def nldi_basin(site):
    """GeoJSON FeatureCollection of the drainage basin upstream of a USGS gauge."""
    return _get(NLDI_BASIN_URL.format(site=site), {"simplified": "false"}).json()


def snotel_stations(bbox):
    """Active SNOTEL stations in bbox (west, south, east, north): [{triplet, name, lon, lat, elevation_m, huc}]."""
    r = _get(f"{AWDB_URL}/stations", params={
        "stationTriplets": "*:*:SNTL", "activeOnly": "true", "returnForecastPointMetadata": "false",
        "returnReservoirMetadata": "false", "returnStationElements": "false"})
    w, s, e, n = bbox
    return [{"triplet": st["stationTriplet"], "name": st["name"], "lon": st["longitude"], "lat": st["latitude"],
             "elevation_m": round(st["elevation"] * FT_TO_M, 1) if st.get("elevation") is not None else None,
             "huc": st.get("huc")}
            for st in r.json() if w <= st["longitude"] <= e and s <= st["latitude"] <= n]


def snotel_daily(triplets, begin, end, elements=("WTEQ", "PRCP", "TAVG")):
    """Daily SNOTEL values: {triplet: {element: {'YYYY-MM-DD': value or None}}}, inches and degF,
    Pacific Standard Time calendar days (WTEQ is the end-of-day reading)."""
    out = {}
    for i in range(0, len(triplets), 50):
        r = _get(f"{AWDB_URL}/data", params={"stationTriplets": ",".join(triplets[i:i + 50]),
                                             "elements": ",".join(elements), "duration": "DAILY",
                                             "beginDate": str(begin), "endDate": str(end)})
        for st in r.json():
            merge_awdb_station(out.setdefault(st["stationTriplet"], {}), st)
    return out


def merge_awdb_station(series, station):
    """Add one AWDB station's data blocks to series ({element: {date: value}}). A station can report two
    sensors for one element (ordinal 1, 2, ...); the primary sensor (lowest ordinal) is kept."""
    ordinals = {}
    for block in station.get("data", []):
        el = block["stationElement"]["elementCode"]
        ordinal = block["stationElement"].get("ordinal", 1)
        if el in series and ordinals.get(el, 1) <= ordinal:
            continue
        ordinals[el] = ordinal
        series[el] = {v["date"][:10]: v.get("value") for v in block.get("values", [])}
    return series
