"""Vectorized geometry helpers (numpy) for working with route polylines."""

import numpy as np
from scipy.spatial import cKDTree

EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles. Accepts scalars or numpy arrays."""
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(v, dtype=float)) for v in (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def cumulative_miles(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Cumulative distance (miles) along a polyline, starting at 0."""
    if len(lats) < 2:
        return np.zeros(len(lats))
    seg = haversine_miles(lats[:-1], lons[:-1], lats[1:], lons[1:])
    return np.concatenate(([0.0], np.cumsum(seg)))


def resample_polyline(lats, lons, cum, step_miles=1.0):
    """Points spaced `step_miles` apart along the polyline (plus the end point)."""
    total = float(cum[-1])
    if total <= 0:
        return np.array(lats[:1]), np.array(lons[:1]), np.zeros(1)
    targets = np.append(np.arange(0.0, total, step_miles), total)
    return np.interp(targets, cum, lats), np.interp(targets, cum, lons), targets


def to_unit_vectors(lats, lons):
    """Lat/lng (degrees) to 3D points on the unit sphere."""
    lat = np.radians(np.asarray(lats, dtype=float))
    lon = np.radians(np.asarray(lons, dtype=float))
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


def locate_points_along_route(route_lats, route_lons, route_miles, pt_lats, pt_lons, max_offset_miles):
    """Project points onto a densely sampled route.

    Returns (indices, mile_markers, offsets) for the points that lie within
    `max_offset_miles` of the route. `mile_markers` is the distance along the
    route of the nearest route sample; `offsets` is the great-circle distance
    from the point to it.

    A KD-tree over the route samples (as 3D unit vectors, so chord distance is
    exact) answers all ~6.6k nearest-neighbour queries in a few milliseconds.
    """
    if len(pt_lats) == 0 or len(route_lats) == 0:
        return np.array([], dtype=int), np.array([]), np.array([])

    tree = cKDTree(to_unit_vectors(route_lats, route_lons))
    max_chord = 2 * np.sin(max_offset_miles / EARTH_RADIUS_MILES / 2)
    chord, nearest = tree.query(to_unit_vectors(pt_lats, pt_lons), distance_upper_bound=max_chord)
    ok = np.isfinite(chord)
    offsets = 2 * EARTH_RADIUS_MILES * np.arcsin(np.clip(chord[ok] / 2, 0.0, 1.0))
    return np.nonzero(ok)[0], np.asarray(route_miles)[nearest[ok]], offsets


def simplify_polyline(lats, lons, tolerance_deg=0.001):
    """Ramer-Douglas-Peucker simplification; returns a boolean keep-mask.

    The default tolerance (~100 m) is invisible at route-overview zoom but
    shrinks a cross-country route from ~35k points to under 2k, keeping the
    API response small.
    """
    n = len(lats)
    keep = np.zeros(n, dtype=bool)
    if n <= 2:
        keep[:] = True
        return keep
    keep[0] = keep[-1] = True
    pts = np.column_stack((lons, lats))
    stack = [(0, n - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        a, b = pts[first], pts[last]
        seg = pts[first + 1:last]
        ab = b - a
        norm = np.hypot(ab[0], ab[1])
        if norm == 0:
            dists = np.hypot(seg[:, 0] - a[0], seg[:, 1] - a[1])
        else:
            dists = np.abs(ab[0] * (seg[:, 1] - a[1]) - ab[1] * (seg[:, 0] - a[0])) / norm
        i = int(np.argmax(dists))
        if dists[i] > tolerance_deg:
            split = first + 1 + i
            keep[split] = True
            stack.append((first, split))
            stack.append((split, last))
    return keep
