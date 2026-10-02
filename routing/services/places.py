"""Local (offline) geocoding of US city names using the Census gazetteer.

The gazetteer gives an internal-point latitude/longitude for every US place
and county subdivision. Matching the fuel file's "City, ST" values against it
lets us geocode all stations at import time, and lets the API resolve
"City, ST" inputs without calling any external service.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
STATE_NAME_TO_CODE = {name.lower(): code for code, name in US_STATES.items()}

# Legal/statistical area descriptions the gazetteer appends to names,
# e.g. "Abbeville city", "Abanda CDP", "Nashville-Davidson metropolitan government (balance)".
_LSAD_SUFFIXES = [
    "consolidated government", "unified government", "metropolitan government",
    "metro government", "urban county", "city and borough", "charter township",
    "municipality", "plantation", "township", "borough", "village", "corporation",
    "town", "city", "cdp",
]
_SUFFIX_RE = re.compile(r"\s+(?:%s)$" % "|".join(re.escape(s) for s in _LSAD_SUFFIXES))
_WORD_SUBSTITUTIONS = [
    (re.compile(r"\bsaint\b"), "st"),
    (re.compile(r"\bsainte\b"), "ste"),
    (re.compile(r"\bfort\b"), "ft"),
    (re.compile(r"\bmount\b"), "mt"),
]


def _clean(name: str, strip_lsad: bool) -> str:
    s = name.lower().strip()
    if strip_lsad:
        # Gazetteer names carry exactly one LSAD term ("Iowa City city"), so
        # strip it once only.
        s = s.replace("(balance)", "").strip()
        s = _SUFFIX_RE.sub("", s).strip() or s
    s = re.sub(r"[.'`’]", "", s)
    s = re.sub(r"[-/]", " ", s)
    for pattern, replacement in _WORD_SUBSTITUTIONS:
        s = pattern.sub(replacement, s)
    return re.sub(r"\s+", " ", s).strip()


# For display only: county divisions ("Pecos CCD") are not used to match towns
# (their centroid can be far from the town), but should still read cleanly.
_DISPLAY_SUFFIX_RE = re.compile(
    r"\s+(?:%s)$" % "|".join(re.escape(s) for s in _LSAD_SUFFIXES + ["ccd", "precinct"]), re.IGNORECASE
)


def display_place_name(name: str) -> str:
    """Census name without its LSAD suffix, keeping case: "Iowa City city" -> "Iowa City"."""
    s = name.replace("(balance)", "").strip()
    return _DISPLAY_SUFFIX_RE.sub("", s).strip() or s


def place_key(name: str, gazetteer_name: bool = False) -> str:
    """Normalize a place name to a lookup key.

    Spaces are removed so "Mc Calla"/"McCalla", "De Forest"/"DeForest" and
    "La Salle"/"LaSalle" all match. Pass gazetteer_name=True for names from
    the Census files, which end in an LSAD term such as "city" or "CDP".
    """
    return _clean(name, strip_lsad=gazetteer_name).replace(" ", "")


def place_key_aliases(name: str) -> list[str]:
    """Extra keys for consolidated cities, e.g. "Nashville-Davidson ..." -> "nashville"."""
    if "-" not in name:
        return []
    alias = place_key(name.split("-", 1)[0])
    return [alias] if alias and alias != place_key(name, gazetteer_name=True) else []


@dataclass(frozen=True)
class GazetteerEntry:
    state: str
    name: str
    key: str
    latitude: float
    longitude: float
    source: str
    # Sort key: lower is preferred when keys collide.
    priority: tuple
    display_name: str = ""
    land_area: int = 0
    search_tier: int = 0


def read_gazetteer(path: Path, source: str, tier: int):
    """Yield GazetteerEntry rows (including consolidated-city aliases) from a gazetteer file."""
    with open(path, encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter="\t")
        header = [h.strip() for h in next(reader)]
        for raw in reader:
            row = dict(zip(header, (v.strip() for v in raw)))
            state = row["USPS"]
            if state not in US_STATES:
                continue
            name = row["NAME"]
            lat, lng = float(row["INTPTLAT"]), float(row["INTPTLONG"])
            aland = int(row.get("ALAND") or 0)
            key = place_key(name, gazetteer_name=True)
            display = display_place_name(name)
            # FUNCSTAT "S" marks statistical areas (CDPs) rather than incorporated places.
            search_tier = 2 if tier > 0 else (1 if row.get("FUNCSTAT") == "S" else 0)
            extra = (aland, search_tier)
            if key:
                yield GazetteerEntry(state, name, key, lat, lng, source, (tier, 0, -aland), display, *extra)
            for alias in place_key_aliases(name):
                # "Nashville-Davidson ..." is offered as plain "Nashville".
                head = display.split("-", 1)[0].strip()
                yield GazetteerEntry(state, name, alias, lat, lng, source, (tier, 1, -aland), head, *extra)


def build_place_index(paths_and_sources) -> dict:
    """Return {(state, key): GazetteerEntry} keeping the preferred entry per key."""
    index: dict = {}
    for tier, (path, source) in enumerate(paths_and_sources):
        for entry in read_gazetteer(path, source, tier):
            existing = index.get((entry.state, entry.key))
            if existing is None or entry.priority < existing.priority:
                index[(entry.state, entry.key)] = entry
    return index


def normalize_state(value: str) -> str | None:
    """Return a two-letter USPS code for a state code or full state name."""
    v = value.strip().rstrip(".")
    if v.upper() in US_STATES:
        return v.upper()
    return STATE_NAME_TO_CODE.get(v.lower())
