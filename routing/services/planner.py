"""Build a full trip plan: route, fuel stops and total fuel cost.

External calls per uncached request: one OSRM route call, plus a Nominatim
call only for inputs that are neither coordinates nor "City, ST". Identical
requests are served from the cache with zero external calls.
"""

import hashlib
import json
import logging
import time

import numpy as np
from django.conf import settings
from django.core.cache import cache

from . import geometry
from .external import CallLog, fetch_route
from .geocoding import resolve_location
from .optimizer import StationOnRoute, plan_fuel_stops
from .station_index import get_station_index

logger = logging.getLogger(__name__)

RESAMPLE_STEP_MILES = 1.0
PLAN_CACHE_VERSION = "plan:v1"
ROUTE_CACHE_VERSION = "route:v1"


def _conf(name):
    return settings.FUEL_ROUTE[name]


def _plan_cache_key(start, finish, start_fuel_gallons, stop_penalty, station_version):
    payload = json.dumps(
        [
            round(start.lat, 5), round(start.lng, 5), round(finish.lat, 5), round(finish.lng, 5),
            round(start_fuel_gallons, 3), round(stop_penalty, 2), station_version,
            _conf("CORRIDOR_MILES"), _conf("ORIGIN_FUEL_RADIUS_MILES"),
            _conf("MAX_RANGE_MILES"), _conf("MILES_PER_GALLON"),
        ]
    )
    return f"{PLAN_CACHE_VERSION}:{hashlib.sha1(payload.encode()).hexdigest()}"


def build_trip_plan(start_text, finish_text, start_fuel_gallons=0.0, stop_penalty=None, include_candidates=False):
    """Return a JSON-serializable dict describing the optimal trip.

    Raises LocationError, ExternalServiceError or FuelPlanError.
    """
    t0 = time.perf_counter()
    calls = CallLog()
    start = resolve_location(start_text, calls)
    finish = resolve_location(finish_text, calls)
    stations = get_station_index()
    if stop_penalty is None:
        stop_penalty = _conf("STOP_PENALTY_DOLLARS")

    key = _plan_cache_key(start, finish, start_fuel_gallons, stop_penalty, stations.version)
    plan = cache.get(key)
    cached = plan is not None
    if not cached:
        plan = _compute_plan(start, finish, start_fuel_gallons, stop_penalty, stations, calls)
        cache.set(key, plan)

    result = dict(plan)
    if not include_candidates:
        result.pop("candidate_stations", None)
    # Echo this request's inputs (the cached plan may have come from different text).
    result["start"] = start.as_dict()
    result["finish"] = finish.as_dict()
    result["meta"] = {
        **plan["meta"],
        "cached": cached,
        "external_api_calls": {"routing": calls.routing, "geocoding": calls.geocoding, "total": calls.total},
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    return result


def get_route(start, finish, calls):
    """Route between two resolved locations, processed for planning. Cached.

    Holds 1-mile samples along the route (for matching stations) and a
    simplified line (for display) rather than the raw ~35k-point geometry, so
    replanning the same route with other options is cheap.
    """
    key = f"{ROUTE_CACHE_VERSION}:{start.lng:.5f},{start.lat:.5f};{finish.lng:.5f},{finish.lat:.5f}"
    route = cache.get(key)
    if route is not None:
        return route

    raw = fetch_route(start.lat, start.lng, finish.lat, finish.lng, calls)
    coords = np.asarray(raw.coordinates, dtype=float)
    lons, lats = coords[:, 0], coords[:, 1]
    geo_cum = geometry.cumulative_miles(lats, lons)
    # OSRM's road distance is the source of truth; scale geometric miles to match.
    scale = raw.distance_miles / geo_cum[-1] if geo_cum[-1] > 0 else 1.0
    s_lats, s_lons, s_miles = geometry.resample_polyline(lats, lons, geo_cum, RESAMPLE_STEP_MILES)
    keep = geometry.simplify_polyline(lats, lons)
    route = {
        "distance_miles": raw.distance_miles,
        "duration_seconds": raw.duration_seconds,
        "sample_lats": s_lats,
        "sample_lons": s_lons,
        "sample_miles": s_miles * scale,
        "line": [[round(x, 5), round(y, 5)] for x, y in zip(lons[keep].tolist(), lats[keep].tolist())],
    }
    cache.set(key, route)
    return route


def _compute_plan(start, finish, start_fuel_gallons, stop_penalty, stations, calls):
    max_range = _conf("MAX_RANGE_MILES")
    mpg = _conf("MILES_PER_GALLON")
    corridor = _conf("CORRIDOR_MILES")
    origin_radius = _conf("ORIGIN_FUEL_RADIUS_MILES")
    tank_gallons = max_range / mpg
    start_fuel_gallons = min(max(start_fuel_gallons, 0.0), tank_gallons)

    route = get_route(start, finish, calls)
    t_route = time.perf_counter()

    idx, miles, offsets = geometry.locate_points_along_route(
        route["sample_lats"], route["sample_lons"], route["sample_miles"], stations.lats, stations.lons, corridor
    )
    candidates = []
    for i, mile, offset in zip(idx.tolist(), miles.tolist(), offsets.tolist()):
        row = stations.rows[i]
        # With an empty tank the trip begins by fueling near the origin, so
        # stations within `origin_radius` route-miles count as being at mile 0.
        plan_mile = 0.0 if (start_fuel_gallons <= 0 and mile <= origin_radius) else mile
        candidates.append(
            StationOnRoute(
                mile=plan_mile,
                price=row["price"],
                ref={**row, "mile_marker": round(mile, 1), "distance_from_route_miles": round(offset, 1)},
            )
        )

    fuel_plan = plan_fuel_stops(
        candidates,
        route["distance_miles"],
        max_range_miles=max_range,
        mpg=mpg,
        start_fuel_miles=start_fuel_gallons * mpg,
        stop_penalty=stop_penalty,
    )
    t_plan = time.perf_counter()

    stops = []
    for n, purchase in enumerate(fuel_plan.purchases, start=1):
        ref = purchase.station.ref
        stops.append(
            {
                "stop_number": n,
                "opis_id": ref["opis_id"],
                "name": ref["name"],
                "address": ref["address"],
                "city": ref["city"],
                "state": ref["state"],
                "lat": ref["lat"],
                "lng": ref["lng"],
                "mile_marker": ref["mile_marker"],
                "distance_from_route_miles": ref["distance_from_route_miles"],
                "price_per_gallon": round(ref["price"], 4),
                "gallons": round(purchase.gallons, 2),
                "cost": round(purchase.cost, 2),
                "fuel_on_arrival_gallons": round(purchase.fuel_on_arrival_gallons, 2),
                "fuel_on_departure_gallons": round(purchase.fuel_on_departure_gallons, 2),
            }
        )

    total_gallons_needed = route["distance_miles"] / mpg
    return {
        "route": {
            "distance_miles": round(route["distance_miles"], 1),
            "duration_hours": round(route["duration_seconds"] / 3600, 2),
            "geometry": {"type": "LineString", "coordinates": route["line"]},
        },
        "vehicle": {
            "max_range_miles": max_range,
            "miles_per_gallon": mpg,
            "tank_capacity_gallons": tank_gallons,
            "start_fuel_gallons": round(start_fuel_gallons, 2),
        },
        "optimization": {
            "objective": "minimize total fuel cost + stop_penalty x number of stops",
            "stop_penalty": stop_penalty,
        },
        "fuel_stops": stops,
        "summary": {
            "total_fuel_cost": round(fuel_plan.total_cost, 2),
            "total_gallons_purchased": round(fuel_plan.total_gallons, 2),
            "total_gallons_used": round(total_gallons_needed, 2),
            "number_of_stops": len(stops),
            "average_price_per_gallon": (
                round(fuel_plan.total_cost / fuel_plan.total_gallons, 4) if fuel_plan.total_gallons else None
            ),
            "stations_considered": len(candidates),
        },
        "candidate_stations": [
            {k: c.ref[k] for k in ("opis_id", "name", "city", "state", "lat", "lng", "price", "mile_marker")}
            for c in sorted(candidates, key=lambda c: c.ref["mile_marker"])
        ],
        "meta": {
            "routing_provider": "OSRM (router.project-osrm.org)",
            "compute_ms": {
                "route_processing": round((t_plan - t_route) * 1000, 1),
            },
            "assumptions": [
                f"Vehicle range {max_range:.0f} miles, {mpg:.0f} miles per gallon ({tank_gallons:.0f} gallon tank).",
                (
                    f"Tank starts empty; the first fill-up is at a station within {origin_radius:.0f} "
                    "route-miles of the origin."
                    if start_fuel_gallons <= 0
                    else f"Tank starts with {start_fuel_gallons:g} gallons (not included in the cost)."
                ),
                "Vehicle arrives at the destination with an empty tank (no fuel is bought that is not used).",
                f"Each stop is weighted as ${stop_penalty:g} when choosing stops, so the planner skips stops that "
                "save less than that; stop_penalty=0 gives the strictly cheapest plan. The penalty is never "
                "included in total_fuel_cost.",
                f"Stations are located by city and considered on-route within {corridor:.0f} miles of the route; "
                "detour distance to a station is not added to the cost.",
                "Where the price file lists a station more than once, its lowest price is used.",
            ],
        },
    }
