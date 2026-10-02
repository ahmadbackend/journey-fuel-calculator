"""In-memory copy of the fuel stations as numpy arrays.

~6.7k stations fit in a few hundred KB, so each process loads them from
PostgreSQL once and does all spatial filtering in numpy. The import command
bumps a version number in the shared cache, so running processes reload after
a re-import.
"""

import threading
from dataclasses import dataclass

import numpy as np
from django.core.cache import cache

from ..models import FuelStation

VERSION_CACHE_KEY = "stations:version"

_lock = threading.Lock()
_index = None


@dataclass
class StationIndex:
    version: str
    lats: np.ndarray
    lons: np.ndarray
    prices: np.ndarray
    rows: list  # dicts with id/name/address/city/state/price/lat/lng

    def __len__(self):
        return len(self.rows)


def current_version():
    return str(cache.get(VERSION_CACHE_KEY) or "0")


def bump_version():
    import time

    cache.set(VERSION_CACHE_KEY, str(time.time_ns()), timeout=None)


def _load(version):
    rows = list(
        FuelStation.objects.values("opis_id", "name", "address", "city", "state", "price", "latitude", "longitude")
    )
    return StationIndex(
        version=version,
        lats=np.array([r["latitude"] for r in rows], dtype=float),
        lons=np.array([r["longitude"] for r in rows], dtype=float),
        prices=np.array([float(r["price"]) for r in rows], dtype=float),
        rows=[
            {
                "opis_id": r["opis_id"],
                "name": r["name"],
                "address": r["address"],
                "city": r["city"],
                "state": r["state"],
                "price": float(r["price"]),
                "lat": r["latitude"],
                "lng": r["longitude"],
            }
            for r in rows
        ],
    )


def get_station_index() -> StationIndex:
    global _index
    version = current_version()
    if _index is not None and _index.version == version:
        return _index
    with _lock:
        if _index is None or _index.version != version:
            _index = _load(version)
    return _index


def reset_station_index():
    global _index
    with _lock:
        _index = None
