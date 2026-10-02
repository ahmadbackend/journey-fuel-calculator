"""HTTP clients for the free map services, with caching and call counting.

* Routing: OSRM public server (https://project-osrm.org), no API key.
  Exactly one call per uncached route.
* Geocoding: Nominatim (OpenStreetMap), no API key. Only used when an input
  cannot be resolved from coordinates or the local Census gazetteer.
"""

import hashlib
import logging
import threading
import time
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_session = requests.Session()
_nominatim_lock = threading.Lock()
_last_nominatim_call = 0.0


class ExternalServiceError(Exception):
    """The routing/geocoding provider failed or returned no result."""


@dataclass
class CallLog:
    """Counts external API calls made while serving one request."""

    routing: int = 0
    geocoding: int = 0

    @property
    def total(self):
        return self.routing + self.geocoding


def _conf(name):
    return settings.FUEL_ROUTE[name]


def _get_json(url, params=None):
    try:
        response = _session.get(
            url,
            params=params,
            headers={"User-Agent": _conf("HTTP_USER_AGENT")},
            timeout=_conf("HTTP_TIMEOUT_SECONDS"),
        )
    except requests.RequestException as exc:
        raise ExternalServiceError(f"Could not reach {url.split('/')[2]}: {exc}") from exc
    if response.status_code >= 500 or response.status_code == 429:
        raise ExternalServiceError(f"{url.split('/')[2]} returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise ExternalServiceError(f"{url.split('/')[2]} returned invalid JSON") from exc


@dataclass
class RouteData:
    distance_miles: float
    duration_seconds: float
    coordinates: list  # [[lng, lat], ...] (GeoJSON order)


class NoRouteError(Exception):
    """The routing provider found no drivable route between the points."""


def fetch_route(start_lat, start_lng, end_lat, end_lng, calls: CallLog) -> RouteData:
    """Driving route between two points: exactly one OSRM call.

    Callers cache the processed result (see planner.get_route).
    """
    coords = f"{start_lng:.5f},{start_lat:.5f};{end_lng:.5f},{end_lat:.5f}"
    url = f"{_conf('OSRM_BASE_URL').rstrip('/')}/route/v1/driving/{coords}"
    calls.routing += 1
    started = time.perf_counter()
    data = _get_json(url, {"overview": "full", "geometries": "geojson", "steps": "false", "alternatives": "false"})
    logger.info("OSRM route %s took %.0f ms", coords, (time.perf_counter() - started) * 1000)

    code = data.get("code")
    if code in ("NoRoute", "NoSegment") or (code == "Ok" and not data.get("routes")):
        raise NoRouteError(data.get("message") or "No drivable route between these locations.")
    if code != "Ok":
        raise ExternalServiceError(data.get("message") or f"Routing failed ({code}).")
    route = data["routes"][0]
    return RouteData(
        distance_miles=route["distance"] / 1609.344,
        duration_seconds=route["duration"],
        coordinates=route["geometry"]["coordinates"],
    )


def geocode_nominatim(query=None, calls: CallLog | None = None, *, city=None, state=None):
    """Geocode within the USA. Returns dict(lat, lng, label) or None. Cached.

    Pass free text as `query`, or `city` + `state` for a structured lookup
    (which avoids matching a same-named county instead of the town).
    """
    if query:
        params = {"q": query}
    else:
        params = {"city": city, "state": state, "country": "USA"}
    normalized = " ".join(repr(sorted(params.items())).lower().split())
    cache_key = "nominatim:v1:" + hashlib.sha1(normalized.encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None

    global _last_nominatim_call
    with _nominatim_lock:
        # Nominatim usage policy: at most one request per second.
        wait = 1.0 - (time.monotonic() - _last_nominatim_call)
        if wait > 0:
            time.sleep(wait)
        if calls is not None:
            calls.geocoding += 1
        try:
            data = _get_json(
                f"{_conf('NOMINATIM_BASE_URL').rstrip('/')}/search",
                {**params, "format": "jsonv2", "limit": 1, "countrycodes": "us", "addressdetails": 0},
            )
        finally:
            _last_nominatim_call = time.monotonic()

    result = None
    if isinstance(data, list) and data:
        hit = data[0]
        result = {"lat": float(hit["lat"]), "lng": float(hit["lon"]), "label": hit.get("display_name", query or city)}
    cache.set(cache_key, result or {}, timeout=60 * 60 * 24 * 30)
    return result
