import json
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import render
from django.urls import reverse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RouteRequestSerializer
from .services.external import ExternalServiceError, NoRouteError
from .services.geocoding import LocationError
from .services.optimizer import FuelPlanError
from .services.place_search import search_places
from .services.planner import build_trip_plan
from .services.station_index import get_station_index

logger = logging.getLogger(__name__)


def _plan_or_error(data):
    """Run the planner; return (plan, None) or (None, (error_body, http_status))."""
    try:
        plan = build_trip_plan(
            data["start"],
            data["finish"],
            start_fuel_gallons=data["start_fuel_gallons"],
            stop_penalty=data["stop_penalty"],
            include_candidates=data["include_candidates"],
        )
        return plan, None
    except LocationError as exc:
        return None, ({"error": "invalid_location", "detail": str(exc)}, status.HTTP_400_BAD_REQUEST)
    except NoRouteError as exc:
        return None, ({"error": "no_route", "detail": str(exc)}, status.HTTP_422_UNPROCESSABLE_ENTITY)
    except FuelPlanError as exc:
        body = {
            "error": "route_not_fuelable",
            "detail": str(exc),
            "gap_start_mile": exc.gap_start_mile,
            "gap_end_mile": exc.gap_end_mile,
        }
        if exc.gap_start_mile == 0:
            body["hint"] = "Pass start_fuel_gallons > 0 if the vehicle starts with fuel in the tank."
        return None, (body, status.HTTP_422_UNPROCESSABLE_ENTITY)
    except ExternalServiceError as exc:
        logger.warning("Routing provider error: %s", exc)
        return None, ({"error": "routing_unavailable", "detail": str(exc)}, status.HTTP_502_BAD_GATEWAY)


class RoutePlanView(APIView):
    """Plan a trip between two US locations with the cheapest fuel stops.

    GET  /api/route/?start=Chicago, IL&finish=Denver, CO
    POST /api/route/  {"start": "...", "finish": "...", "start_fuel_gallons": 0}

    Returns the route geometry (GeoJSON), the fuel stops, the total fuel cost
    and a `map_url` that renders the result on an interactive map.
    """

    def get(self, request):
        return self._handle(request, request.query_params)

    def post(self, request):
        return self._handle(request, request.data)

    def _handle(self, request, payload):
        serializer = RouteRequestSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        plan, error = _plan_or_error(data)
        if error:
            body, code = error
            return Response(body, status=code)

        query = {"start": data["start"], "finish": data["finish"]}
        for optional in ("start_fuel_gallons", "stop_penalty"):
            if data[optional] is not None and optional in payload:
                query[optional] = data[optional]
        plan["map_url"] = request.build_absolute_uri(f"{reverse('route-map')}?{urlencode(query)}")
        return Response(plan)


def route_map(request):
    """Interactive Leaflet map of the route and fuel stops.

    Uses the same cached plan as the JSON API, so opening the map right after
    an API call makes no additional routing calls.
    """
    form = {"start": "", "finish": "", "start_fuel_gallons": "", "stop_penalty": ""}
    conf = settings.FUEL_ROUTE
    context = {
        "plan": None,
        "error": None,
        "form": form,
        # json.dumps gives safely quoted JS string literals.
        "tile_url": json.dumps(conf["MAP_TILE_URL"]),
        "tile_attribution": json.dumps(conf["MAP_TILE_ATTRIBUTION"]),
    }
    if "start" in request.GET or "finish" in request.GET:
        # Blank optional form fields mean "use the default".
        payload = {k: v for k, v in request.GET.items() if v.strip() or k in ("start", "finish")}
        payload["include_candidates"] = True
        context["form"].update({k: request.GET.get(k, "") for k in context["form"]})
        serializer = RouteRequestSerializer(data=payload)
        if serializer.is_valid():
            plan, error = _plan_or_error(serializer.validated_data)
            context["plan"] = plan
            if error:
                context["error"] = error[0]["detail"]
        else:
            context["error"] = "; ".join(f"{k}: {' '.join(map(str, v))}" for k, v in serializer.errors.items())
    return render(request, "routing/map.html", context)


class PlaceSearchView(APIView):
    """Autocomplete: GET /api/places/?q=chic -> US cities, best match first. No external calls."""

    def get(self, request):
        try:
            limit = min(max(int(request.query_params.get("limit", 8)), 1), 20)
        except ValueError:
            limit = 8
        return Response({"results": search_places(request.query_params.get("q", ""), limit=limit)})


class HealthView(APIView):
    def get(self, request):
        return Response({"status": "ok", "stations_loaded": len(get_station_index())})
