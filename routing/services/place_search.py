"""Autocomplete for US city names, served from the local Census place table.

No external calls: suggestions come from PostgreSQL with an indexed prefix
search on the normalized name key.
"""

import re

from ..models import Place
from .places import US_STATES, normalize_state, place_key

MIN_QUERY_LENGTH = 2


def _states_matching(prefix):
    """State codes whose code or full name starts with `prefix` ("i" -> IA, ID, IL, IN)."""
    p = prefix.strip().lower().rstrip(".")
    if not p:
        return None
    exact = normalize_state(p)
    if exact:
        return [exact]
    return [code for code, name in US_STATES.items() if name.lower().startswith(p)]


def search_places(query, limit=8):
    """Return [{label, name, state, lat, lng}] best-first for a partial "City[, ST]" query."""
    text = re.sub(r"\s+", " ", (query or "")).strip()
    if len(text) < MIN_QUERY_LENGTH:
        return []

    states = None
    if "," in text:
        text, _, state_part = text.partition(",")
        states = _states_matching(state_part)
        if states == []:
            return []
    else:
        # "Chicago IL" without a comma.
        head, _, tail = text.rpartition(" ")
        if head and len(tail) == 2 and normalize_state(tail):
            text, states = head, [normalize_state(tail)]

    key = place_key(text)
    if not key:
        return []
    qs = Place.objects.filter(key__startswith=key)
    if states:
        qs = qs.filter(state__in=states)
    qs = qs.order_by("search_tier", "-land_area")

    results, seen = [], set()
    for p in qs.only("state", "name", "display_name", "latitude", "longitude")[: limit * 3]:
        name = p.display_name or p.name
        label = f"{name}, {p.state}"
        point = (round(p.latitude, 3), round(p.longitude, 3))
        if label in seen or point in seen:
            continue
        seen.update((label, point))
        results.append({"label": label, "name": name, "state": p.state, "lat": p.latitude, "lng": p.longitude})
        if len(results) >= limit:
            break
    return results
