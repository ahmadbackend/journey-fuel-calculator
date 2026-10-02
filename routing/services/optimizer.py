"""Optimal fuel purchasing along a fixed route.

Problem: stations sit at known mile markers along the route with known
prices; the tank holds `max_range` miles of fuel; fuel can be bought in any
amount. Minimize

    total fuel cost + stop_penalty * number_of_stops

`stop_penalty` (dollars) captures that every stop costs time. With
stop_penalty = 0 the result is the strictly cheapest plan; a small penalty
removes pointless top-ups (stopping to save a few cents) at almost no fuel
cost.

Algorithm (exact dynamic program). For any fixed set of stops, an optimal
plan buys at each stop either a full tank or exactly enough to reach the next
stop (Khuller, Malekian & Mestre, "To fill or not to fill", 2007). So the fuel
on arrival at station v is either 0 or R - d(w, v) for the previous station w
where the tank was filled. With

    best0[v] = cheapest way to arrive at v with an empty tank
    F[w]     = cheapest way to leave w with a full tank

all transitions within one tank range can be evaluated with small numpy
matrices. Total work is O(n * k^2) with k = stations within one tank range,
a few milliseconds for a cross-country route.

`greedy_fuel_plan` is the classic greedy for stop_penalty = 0. It is kept as a
reference implementation that the tests compare the DP against.

Everything here is plain Python/numpy with no Django dependency.
"""

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

EPS = 1e-7


class FuelPlanError(Exception):
    """The trip cannot be completed with the available stations."""

    def __init__(self, message, *, gap_start_mile=None, gap_end_mile=None):
        super().__init__(message)
        self.gap_start_mile = gap_start_mile
        self.gap_end_mile = gap_end_mile


@dataclass(frozen=True)
class StationOnRoute:
    mile: float  # distance along the route used for planning
    price: float  # price per gallon
    ref: Any = field(default=None, compare=False)  # caller payload (station dict, id, ...)


@dataclass
class FuelPurchase:
    station: StationOnRoute
    gallons: float
    cost: float
    fuel_on_arrival_gallons: float
    fuel_on_departure_gallons: float


@dataclass
class FuelPlan:
    purchases: list
    total_gallons: float
    total_cost: float


def _prepare(stations, total_miles, max_range_miles, start_fuel_miles):
    """Sort stations, drop those off the route, keep the cheapest per mile marker."""
    best = {}
    for s in stations:
        if -EPS <= s.mile < total_miles - EPS:
            key = round(max(s.mile, 0.0), 3)
            if key not in best or s.price < best[key].price:
                best[key] = s
    nodes = [best[k] for k in sorted(best)]
    if start_fuel_miles <= EPS and (not nodes or nodes[0].mile > EPS):
        raise FuelPlanError(
            "The tank starts empty and there is no fuel station at the origin.",
            gap_start_mile=0.0,
            gap_end_mile=nodes[0].mile if nodes else total_miles,
        )
    return nodes


def _raise_gap(nodes, total_miles, max_range_miles, start_fuel_miles):
    """Explain why no plan exists: the first stretch that cannot be covered."""
    if nodes and start_fuel_miles > EPS and nodes[0].mile > start_fuel_miles + EPS:
        raise FuelPlanError(
            f"The starting fuel ({start_fuel_miles:.0f} miles of range) does not reach the first station "
            f"at mile {nodes[0].mile:.1f}.",
            gap_start_mile=0.0,
            gap_end_mile=nodes[0].mile,
        )
    marks = [n.mile for n in nodes] + [total_miles]
    for a, b in zip(marks, marks[1:]):
        if b - a > max_range_miles + EPS:
            raise FuelPlanError(
                f"No fuel station within {max_range_miles:.0f} miles after mile {a:.1f} (next one is at mile {b:.1f}).",
                gap_start_mile=a,
                gap_end_mile=b,
            )
    raise FuelPlanError("No feasible fueling plan for this route.")


def _simulate(stops, total_miles, max_range_miles, mpg, start_fuel_miles):
    """Turn [(station, 'fill'|'exact', next_mile)] into FuelPurchase records."""
    purchases = []
    fuel, here = start_fuel_miles, 0.0
    for station, action, next_mile in stops:
        fuel -= station.mile - here
        here = station.mile
        if fuel < -1e-6:
            raise AssertionError("internal error: plan runs out of fuel")
        fuel = max(fuel, 0.0)
        target = max_range_miles if action == "fill" else min(next_mile - here, max_range_miles)
        buy = target - fuel
        if buy > 1e-6:
            gallons = buy / mpg
            purchases.append(
                FuelPurchase(station, gallons, gallons * station.price, fuel / mpg, target / mpg)
            )
            fuel = target
    if fuel - (total_miles - here) < -1e-6:
        raise AssertionError("internal error: plan does not reach the destination")
    total_gallons = sum(p.gallons for p in purchases)
    return FuelPlan(purchases, total_gallons, sum(p.cost for p in purchases))


def plan_fuel_stops(
    stations, total_miles, *, max_range_miles=500.0, mpg=10.0, start_fuel_miles=0.0, stop_penalty=0.0
):
    """Return the FuelPlan minimizing fuel cost + stop_penalty * stops.

    `stations` are StationOnRoute objects; those outside [0, total_miles) are
    ignored. With start_fuel_miles == 0 there must be a station at mile 0 (the
    caller snaps stations next to the origin to mile 0). The vehicle reaches
    the destination with an empty tank.
    """
    R = float(max_range_miles)
    D = float(total_miles)
    g0 = min(max(float(start_fuel_miles), 0.0), R)
    # A tiny minimum penalty breaks ties between equally cheap plans in favour
    # of fewer stops without measurably changing the cost.
    lam = max(float(stop_penalty), 1e-6)
    if D <= EPS:
        return FuelPlan([], 0.0, 0.0)
    if g0 >= D - EPS:
        return FuelPlan([], 0.0, 0.0)

    nodes = _prepare(stations, D, R, g0)
    n = len(nodes)
    pos = np.array([s.mile for s in nodes], dtype=float)
    cpm = np.array([s.price / mpg for s in nodes], dtype=float)  # $ per mile of fuel

    # Index n is a virtual "filled up" node at mile g0 - R: leaving it with a
    # full tank means arriving at mile m with g0 - m miles of fuel, at no cost.
    fpos = np.append(pos, g0 - R)
    INF = math.inf
    best0 = np.full(n, INF)
    F = np.full(n + 1, INF)
    best0_par = [None] * n  # (v, arrival) with arrival "zero" or w index
    F_par = [None] * n  # "zero" or w index
    if g0 > EPS:
        F[n] = 0.0
    else:
        best0[pos <= EPS] = 0.0
    best_dest, dest_par = INF, None

    def fill_sources(v):
        """Indices w (incl. virtual) whose full tank reaches v with fuel to spare."""
        lo = int(np.searchsorted(pos, pos[v] - R - EPS, side="left"))
        ws = np.arange(lo, v)
        if g0 > EPS and pos[v] <= g0 + EPS:
            ws = np.append(ws, n)
        if len(ws):
            ws = ws[np.isfinite(F[ws]) & (fpos[ws] < pos[v] - EPS)]
        return ws

    for v in range(n):
        ws = fill_sources(v)
        dwv = pos[v] - fpos[ws]  # distance from each fill source to v

        # Arrival states at v: (fuel 0, cost best0[v]) and (fuel R - dwv, cost F[w]).
        # Leave v with a full tank.
        cands = [best0[v] + R * cpm[v]]
        if len(ws):
            fill_w = F[ws] + dwv * cpm[v]
            k = int(np.argmin(fill_w))
            cands.append(fill_w[k])
        k0 = int(np.argmin(cands))
        if np.isfinite(cands[k0]):
            F[v] = cands[k0] + lam
            F_par[v] = "zero" if k0 == 0 else int(ws[k])

        # Buy exactly enough at v to reach the next stop x (arriving empty).
        hi = int(np.searchsorted(pos, pos[v] + R + EPS, side="right"))
        xs = np.arange(v + 1, hi)
        targets = np.append(pos[xs], D) if D - pos[v] <= R + EPS else pos[xs]
        if not len(targets):
            continue
        dvx = targets - pos[v]
        cost = best0[v] + dvx * cpm[v] + lam
        arrival = np.full(len(targets), -1)  # -1 = arrived at v empty
        if len(ws):
            # Arrived with R - d(w,v); still need fuel only if d(w,x) > R.
            dwx = targets[None, :] - fpos[ws][:, None]
            m = np.where(dwx > R + EPS, F[ws][:, None] + (dwx - R) * cpm[v] + lam, INF)
            kw = np.argmin(m, axis=0)
            mw = m[kw, np.arange(len(targets))]
            better = mw < cost
            cost = np.where(better, mw, cost)
            arrival = np.where(better, ws[kw], arrival)

        n_x = len(xs)
        improve = cost[:n_x] < best0[xs]
        for j in np.nonzero(improve)[0]:
            x = int(xs[j])
            best0[x] = cost[j]
            best0_par[x] = (v, "zero" if arrival[j] < 0 else int(arrival[j]))
        if len(targets) > n_x and cost[-1] < best_dest:
            best_dest = float(cost[-1])
            dest_par = (v, "zero" if arrival[-1] < 0 else int(arrival[-1]))

    if not math.isfinite(best_dest):
        _raise_gap(nodes, D, R, g0)

    # Walk back from the destination. Each item is (station v, how we arrived
    # at v); `action` is what was done at v.
    stops = []
    next_mile = D
    item, action = dest_par, "exact"
    while item is not None:
        v, arrival = item
        stops.append((nodes[v], action, next_mile))
        next_mile = pos[v]
        if arrival == "zero":
            # Arrived empty: the previous stop bought exactly enough (None at the origin).
            item, action = best0_par[v], "exact"
        elif arrival == n:
            # Arrived on the starting fuel.
            item = None
        else:
            # Arrived after filling up at `arrival`.
            item, action = (arrival, F_par[arrival]), "fill"
    stops.reverse()
    return _simulate(stops, D, R, mpg, g0)


def greedy_fuel_plan(stations, total_miles, *, max_range_miles=500.0, mpg=10.0, start_fuel_miles=0.0):
    """Classic greedy: provably cheapest when there is no per-stop penalty.

    At a station, if a cheaper station is within one tank, buy just enough to
    reach the first such station; otherwise fill up and go to the cheapest
    station in range.
    """
    R, D = float(max_range_miles), float(total_miles)
    g0 = min(max(float(start_fuel_miles), 0.0), R)
    if D <= EPS or g0 >= D - EPS:
        return FuelPlan([], 0.0, 0.0)
    nodes = _prepare(stations, D, R, g0)
    if g0 > EPS:
        nodes = [StationOnRoute(0.0, math.inf)] + nodes
    destination = StationOnRoute(D, -math.inf)
    nodes.append(destination)

    stops = []
    fuel, i, n = g0, 0, len(nodes)
    while nodes[i] is not destination:
        here = nodes[i]
        can_buy = math.isfinite(here.price)
        reach = here.mile + (R if can_buy else fuel)
        nxt = next(
            (k for k in range(i + 1, n) if nodes[k].mile <= reach + EPS and nodes[k].price < here.price), None
        )
        if nxt is not None:
            need = nodes[nxt].mile - here.mile
            if can_buy:
                stops.append((here, "exact", nodes[nxt].mile))
            fuel = max(fuel, need) - need
        else:
            in_range = [k for k in range(i + 1, n) if nodes[k].mile <= reach + EPS]
            if not in_range or not can_buy:
                _raise_gap([s for s in nodes if math.isfinite(s.price)], D, R, g0)
            nxt = min(in_range, key=lambda k: (nodes[k].price, -nodes[k].mile))
            stops.append((here, "fill", nodes[nxt].mile))
            fuel = R - (nodes[nxt].mile - here.mile)
        i = nxt
    return _simulate(stops, D, R, mpg, g0)
