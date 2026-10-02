"""Turn user input ("Chicago, IL", "41.88,-87.63", an address) into coordinates.

Resolution order, cheapest first:
1. "lat,lng" coordinates - no lookup at all.
2. "City, ST" / "City, State" - local Census gazetteer table in PostgreSQL.
3. Anything else - one Nominatim call (cached), restricted to the USA.
"""

import re
from dataclasses import dataclass

from ..models import Place
from .external import CallLog, geocode_nominatim
from .places import normalize_state, place_key

_COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$")
_ZIP_RE = re.compile(r"\s+\d{5}(?:-\d{4})?$")
_COUNTRY_RE = re.compile(r",\s*(?:usa|us|u\.s\.a?\.?|united states(?: of america)?)\s*$", re.IGNORECASE)

# Rough bounding boxes for the US (contiguous states, Alaska, Hawaii).
_US_BOXES = [
    (24.3, 49.5, -125.0, -66.8),
    (51.0, 71.6, -180.0, -129.9),
    (18.8, 22.4, -160.4, -154.7),
]


class LocationError(ValueError):
    pass


@dataclass
class ResolvedLocation:
    query: str
    label: str
    lat: float
    lng: float
    source: str  # "coordinates" | "gazetteer" | "nominatim"

    def as_dict(self):
        return {
            "query": self.query,
            "label": self.label,
            "lat": round(self.lat, 6),
            "lng": round(self.lng, 6),
            "source": self.source,
        }


def in_usa(lat, lng):
    return any(lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng for lo_lat, hi_lat, lo_lng, hi_lng in _US_BOXES)


def _lookup_city_state(text):
    text = _COUNTRY_RE.sub("", text)
    if "," not in text:
        return None
    city, _, state = text.rpartition(",")
    state = _ZIP_RE.sub("", state.strip())
    code = normalize_state(state)
    if code is None and re.fullmatch(r"[A-Za-z]{2}", state):
        # "Toronto, ON", "Tijuana, BC": a region code that is not a US state.
        raise LocationError(f"'{state.upper()}' is not a US state; both locations must be in the USA.")
    key = place_key(city)
    if not code or not key:
        return None
    place = Place.objects.filter(state=code, key=key).only("name", "latitude", "longitude").first()
    if place is None:
        return None
    return place.latitude, place.longitude, f"{city.strip().title()}, {code}"


def resolve_location(text, calls: CallLog) -> ResolvedLocation:
    query = (text or "").strip()
    if not query:
        raise LocationError("Location is empty.")

    match = _COORD_RE.match(query)
    if match:
        lat, lng = float(match.group(1)), float(match.group(2))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise LocationError(f"'{query}' is not a valid 'latitude,longitude' pair.")
        if not in_usa(lat, lng):
            raise LocationError(f"'{query}' is outside the USA.")
        return ResolvedLocation(query, f"{lat:.5f}, {lng:.5f}", lat, lng, "coordinates")

    local = _lookup_city_state(query)
    if local:
        lat, lng, label = local
        return ResolvedLocation(query, label, lat, lng, "gazetteer")

    hit = geocode_nominatim(query, calls)
    if not hit:
        raise LocationError(f"Could not find '{query}' in the USA. Try 'City, ST' or 'latitude,longitude'.")
    if not in_usa(hit["lat"], hit["lng"]):
        raise LocationError(f"'{query}' resolved outside the USA.")
    return ResolvedLocation(query, hit["label"], hit["lat"], hit["lng"], "nominatim")
