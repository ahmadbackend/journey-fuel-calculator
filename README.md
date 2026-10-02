# Fuel Route Optimizer API

A Django REST API that takes a start and a finish location in the USA and returns:

- the driving route (GeoJSON) plus a link to an interactive map,
- the fuel stops along the way, chosen to keep fuel cost low,
- the total fuel cost for the trip, assuming a 500-mile range and 10 miles per gallon.

Fuel prices come from `fuel-prices-for-be-assessment.csv`. Routing uses the free
[OSRM](https://project-osrm.org) public server. Each new route makes **one** routing call,
and a repeated request makes none.

## How it meets the requirements

| Requirement | How |
|---|---|
| Start/finish in the USA | Accepts `City, ST`, `City, State`, street addresses or `lat,lng`. Rejects non-US places such as `Toronto, ON` or `51.5,-0.12`. |
| Map of the route | `route.geometry` is a GeoJSON LineString, and `map_url` opens an interactive Leaflet/OpenStreetMap page with the route, the numbered stops and every station along the route, colored by price. |
| Cost-effective fuel stops, 500-mile range | An exact optimizer picks the stops; see [Choosing fuel stops](#choosing-fuel-stops). The tank never goes above 50 gallons or below empty, and several stops are planned when the trip needs them. |
| Total money spent at 10 mpg | `summary.total_fuel_cost`; the gallons bought always equal `distance / 10`. |
| Latest stable Django | Django **5.2 LTS** (5.2.17). Django 6.x requires Python 3.12+, and this environment runs Python 3.11. |
| Fast responses | About 1 s on a new route, nearly all of it OSRM's response time. Our own processing takes under about 120 ms. A cached route takes about 20 ms. |
| Few routing API calls | **1 call** per new route. Repeats and option changes reuse the cached route (0 calls). City names and coordinates are geocoded locally (0 calls). |
| PostgreSQL | Stores stations, a US place gazetteer, and the shared cache. Uses Django's native psycopg connection pool. |

## Quick start (Windows, existing venv at `ven\`)

```powershell
# 1. Install dependencies into the existing virtualenv
ven\Scripts\python.exe -m pip install -r requirements.txt

# 2. Create the database (credentials are read from .env; see .env.example)
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -c "CREATE DATABASE fuel_route;"

# 3. Create tables and the cache table
ven\Scripts\python.exe manage.py migrate
ven\Scripts\python.exe manage.py createcachetable

# 4. Import and geocode the fuel stations (about 10 s, works offline)
ven\Scripts\python.exe manage.py load_fuel_stations

# 5. Run
ven\Scripts\python.exe manage.py runserver
```

Then open <http://127.0.0.1:8000/> for the map UI, or call the API:

```powershell
curl "http://127.0.0.1:8000/api/route/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA"
```

Run the tests with `ven\Scripts\python.exe manage.py test routing` (33 tests).

## API

### `GET /api/route/` or `POST /api/route/`

| Parameter | Required | Description |
|---|---|---|
| `start` | yes | `"Chicago, IL"`, `"Chicago, Illinois"`, `"41.8781,-87.6298"` or a street address |
| `finish` | yes | Same formats as `start` |
| `start_fuel_gallons` | no | Fuel already in the tank, 0–50. The default, 0, means the trip starts by filling up near the origin. |
| `stop_penalty` | no | Dollar weight of one stop when choosing stops. Default 5. Use `0` for the strictly cheapest fuel, however many stops that takes. |
| `include_candidates` | no | `true` also returns every station found along the route |

POST takes the same fields as JSON:

```json
{"start": "Chicago, IL", "finish": "Denver, CO", "start_fuel_gallons": 20, "stop_penalty": 5}
```

**Response (abridged), New York, NY to Los Angeles, CA:**

```json
{
  "start":  {"query": "New York, NY", "label": "New York, NY", "lat": 40.662712, "lng": -73.938677, "source": "gazetteer"},
  "finish": {"query": "Los Angeles, CA", "label": "Los Angeles, CA", "lat": 34.019394, "lng": -118.410825, "source": "gazetteer"},
  "route": {
    "distance_miles": 2810.4,
    "duration_hours": 50.31,
    "geometry": {"type": "LineString", "coordinates": [[-73.93868, 40.66271], "..."]}
  },
  "vehicle": {"max_range_miles": 500.0, "miles_per_gallon": 10.0, "tank_capacity_gallons": 50.0, "start_fuel_gallons": 0.0},
  "optimization": {"objective": "minimize total fuel cost + stop_penalty x number of stops", "stop_penalty": 5.0},
  "fuel_stops": [
    {
      "stop_number": 1, "opis_id": 52322, "name": "BOLLA MARKET",
      "address": "I-95, EXIT 13", "city": "Elizabeth", "state": "NJ", "lat": 40.666367, "lng": -74.193503,
      "mile_marker": 15.0, "distance_from_route_miles": 5.6,
      "price_per_gallon": 3.099, "gallons": 39.56, "cost": 122.6,
      "fuel_on_arrival_gallons": 0.0, "fuel_on_departure_gallons": 39.56
    },
    "... 6 more stops ..."
  ],
  "summary": {
    "total_fuel_cost": 860.16,
    "total_gallons_purchased": 281.04,
    "total_gallons_used": 281.04,
    "number_of_stops": 7,
    "average_price_per_gallon": 3.0607,
    "stations_considered": 459
  },
  "map_url": "http://127.0.0.1:8000/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA",
  "meta": {
    "cached": false,
    "external_api_calls": {"routing": 1, "geocoding": 0, "total": 1},
    "elapsed_ms": 905.6,
    "assumptions": ["..."]
  }
}
```

**Errors**

| Status | `error` | When |
|---|---|---|
| 400 | field errors | A required field is missing, or a value is out of range |
| 400 | `invalid_location` | The location is unknown or outside the USA |
| 422 | `no_route` | No drivable route exists, e.g. from Honolulu |
| 422 | `route_not_fuelable` | A stretch longer than 500 miles has no station. The response includes `gap_start_mile` and `gap_end_mile`. |
| 502 | `routing_unavailable` | OSRM or Nominatim is down |

### `GET /api/places/?q=chic`

City autocomplete from the local Census place table, so it makes no external calls and takes a few
milliseconds. Incorporated cities rank first, then larger places. Accepts an optional state: `chic, il`,
`chicago il`, `springfield, mass`. `limit` defaults to 8 and can go up to 20.

```json
{"results": [{"label": "Chicago, IL", "name": "Chicago", "state": "IL", "lat": 41.83, "lng": -87.68}, "..."]}
```

Every `label` can be passed straight to `start` or `finish`. The map page uses this endpoint to suggest
cities as you type.

### Other endpoints

- `GET /map/?start=...&finish=...`: interactive map page. It uses the same cache as the API, so opening a `map_url` makes no extra routing call.
  Map tiles come from Esri's free World Street Map. OpenStreetMap's own tile servers return "Access
  blocked" tiles to apps like this one. To use a different provider, set `MAP_TILE_URL` and
  `MAP_TILE_ATTRIBUTION` in `.env`.
- `GET /api/health/`: `{"status": "ok", "stations_loaded": 6626}`
- `/admin/`: Django admin for stations and places.

## How it works

```
request ─► geocode start/finish ─► get route ─► stations near the route ─► optimizer ─► response
           (local DB, 0 calls)     (OSRM, 1 call,   (KD-tree, ~10 ms)        (DP, ~30-60 ms)
                                    then cached)
```

### 1. Geocoding the stations, once, at import time

The CSV has addresses like `I-44, EXIT 283 & US-69` but no coordinates. Geocoding about 6,600 stations
on each request, or even at import time through a rate-limited API, would be slow. Instead,
`load_fuel_stations` matches each station's city and state against the **US Census Gazetteer**
(places plus county subdivisions, vendored in `data/`). Matching normalizes names, so
`St./Saint`, `Mc Calla/McCalla`, `De Forest/DeForest` and consolidated cities such as
`Nashville-Davidson` all match. Of the 6,626 US stations:

- 6,305 match a Census place,
- 108 match a county subdivision,
- 213 small towns are looked up once with Nominatim. Those answers are saved in `data/geocode_cache.json`, so re-imports run offline.

Rows from Canadian provinces are skipped because routes are US-only. When a station appears several times
with different prices, its lowest price is kept.

The same gazetteer is stored in the `Place` table, so `City, ST` inputs are geocoded with an indexed
database lookup instead of an external call. Only free-form addresses go to Nominatim.

### 2. One routing call

OSRM returns the full route geometry, about 35k points for a cross-country trip. The app processes and
caches it in PostgreSQL as:

- points every 1 mile along the route, used to place stations at a mile marker,
- a simplified line of about 1.7k points (Douglas-Peucker, ~100 m tolerance), used for display.

Requests with the same endpoints but other options (`stop_penalty`, `start_fuel_gallons`) reuse the cached route.

### 3. Finding stations along the route

All stations are held in memory as numpy arrays, loaded once per process. A `scipy` KD-tree built over
the 1-mile route points, with points converted to 3D unit vectors so distances are exact, finds every
station within `CORRIDOR_MILES` (10) of the route and its mile marker in about 10 ms. The corridor is
wider than a typical highway exit because each station is placed at its city's center.

### Choosing fuel stops

This is the classic gas-station problem: stations sit at known mile markers with known prices, the
tank holds 500 miles of fuel, and any amount can be bought. The cheapest plan follows a known greedy
rule: at each station, buy just enough to reach the next cheaper station, or fill up if no cheaper
station is within range.

Taken literally, that plan is impractical. From New York to Los Angeles it makes **18 stops**, including
1.2-gallon top-ups that save a cent. So the optimizer minimizes

```
total fuel cost + stop_penalty × number of stops
```

as an **exact dynamic program**. For any fixed set of stops, an optimal plan buys either a full tank or
exactly enough to reach the next stop (Khuller, Malekian & Mestre, *To fill or not to fill*, 2007). So
the fuel left on arrival at a station is either 0 or `500 − distance from the last fill-up`, and the DP
only has to track those two cases. It runs in a few tens of milliseconds for a cross-country route.

The default penalty of $5 per stop produces:

| stop_penalty | Cost NY→LA | Stops |
|---|---|---|
| 0 (strictly cheapest) | $852.47 | 18 |
| **5 (default)** | **$860.16** | **7** |
| 50 | $909.39 | 6 |

That is 0.9% more fuel cost for 11 fewer stops. The penalty only affects which stops are chosen;
`total_fuel_cost` is always the actual fuel spend. With `stop_penalty=0`, the tests check the DP against
the greedy rule on hundreds of random instances.

### Assumptions

These are also returned in `meta.assumptions`.

- **Starting fuel.** By default the tank starts empty and the trip begins by fueling at a station within 30 route-miles of the origin. That station is treated as mile 0. As a result, `total_fuel_cost` covers every mile driven: gallons bought = distance / 10. Pass `start_fuel_gallons` if the vehicle starts with fuel; that fuel is not counted in the cost.
- **Arrival.** The vehicle arrives with an empty tank, so no fuel is bought that is not used.
- **Detours.** The detour from the route to a station (a few miles at most) is not added to the cost.

## Performance

Measured locally on Windows with the threaded dev server:

| Case | Time |
|---|---|
| New route (includes the OSRM round trip of about 0.8 s) | about 0.9–1.3 s |
| Same endpoints, different options (route cached) | about 70–110 ms |
| Repeated request (plan cached) | about 20 ms |

In production, point `OSRM_BASE_URL` at a self-hosted OSRM server for higher throughput. The public
demo server has fair-use limits.

## Project layout

```
config/                     Django settings and URLs (settings read from .env)
routing/
  models.py                 FuelStation, Place
  views.py                  API view, map page, health check
  serializers.py            Input validation
  services/
    places.py               Census gazetteer parsing and name normalization
    place_search.py         City autocomplete (/api/places/)
    geocoding.py            User input to coordinates (coords, local gazetteer, then Nominatim)
    external.py             OSRM and Nominatim clients (call counting, Nominatim rate limit)
    geometry.py             Resampling, KD-tree station matching, simplification
    station_index.py        In-memory numpy station index (reloads after a re-import)
    optimizer.py            DP optimizer and the greedy reference version
    planner.py              Ties it all together, with caching
  management/commands/load_fuel_stations.py
  templates/routing/map.html  Leaflet map UI
  tests/                    Optimizer, geometry, API (OSRM mocked) and import tests
data/                       Census gazetteer files and the Nominatim fallback cache
```
