"""
Fetches official, freely-licensed open data from data.nola.gov (Socrata /
SODA API) - parcel geometry and neighborhood boundaries. No scraping: this
is a public, paginated, rate-limit-friendly REST API meant for bulk
consumers.
"""
from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger(__name__)

SOCRATA_BASE = "https://data.nola.gov/resource"
PARCELS_DATASET = "pzqp-4ri7"  # "Parcels To Join" - GEOPIN + PARID + polygon, updated monthly
NEIGHBORHOODS_DATASET = "exvn-jeh2"  # "Neighborhood Statistical Area"
ZONING_DATASET = "bizp-xi7c"  # "Zoning Districts"

PAGE_SIZE = 5000
REQUEST_DELAY_SECONDS = 0.3
USER_AGENT = "nola-tax-assessor-scraper/0.1 (civic-data project)"


def _paged_get(dataset_id: str, session: requests.Session):
    offset = 0
    while True:
        url = f"{SOCRATA_BASE}/{dataset_id}.json"
        params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
        resp = session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            return
        yield from batch
        offset += len(batch)
        if len(batch) < PAGE_SIZE:
            return
        time.sleep(REQUEST_DELAY_SECONDS)


def fetch_parcels(conn_upsert, session: requests.Session | None = None) -> int:
    """conn_upsert(parcel_id, geopin, geojson_str) is called once per row."""
    session = session or requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    n = 0
    for row in _paged_get(PARCELS_DATASET, session):
        geom = row.get("the_geom")
        parid = row.get("parid")
        geopin = row.get("geopin")
        if not geom or not parid:
            continue
        conn_upsert(parid, geopin, json.dumps(geom))
        n += 1
        if n % 20000 == 0:
            log.info("fetched %d parcels so far", n)
    return n


def fetch_neighborhoods(conn_upsert, session: requests.Session | None = None) -> int:
    """conn_upsert(neigh_id, name, geojson_str) is called once per row."""
    session = session or requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    n = 0
    for row in _paged_get(NEIGHBORHOODS_DATASET, session):
        geom = row.get("the_geom")
        name = row.get("gnocdc_lab")
        neigh_id = row.get("neigh_id")
        if not geom or not name:
            continue
        conn_upsert(neigh_id, name, json.dumps(geom))
        n += 1
    return n


def fetch_zoning(conn_upsert, session: requests.Session | None = None) -> int:
    """conn_upsert(zone_class, zone_desc, geojson_str) is called once per row."""
    session = session or requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    n = 0
    for row in _paged_get(ZONING_DATASET, session):
        geom = row.get("the_geom")
        zone_class = row.get("zoneclass")
        zone_desc = row.get("zonedesc")
        if not geom or not zone_class:
            continue
        conn_upsert(zone_class, zone_desc, json.dumps(geom))
        n += 1
    return n
