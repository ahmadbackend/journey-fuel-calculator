import csv
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import numpy as np
from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from routing.models import FuelStation, Place
from routing.services import geometry
from routing.services.external import NoRouteError, RouteData
from routing.services.places import place_key
from routing.services.station_index import bump_version, reset_station_index

# A synthetic east-west route along latitude 40, from longitude -104 to -90 (~740 miles).
ROUTE_LONS = np.linspace(-104, -90, 1401)
ROUTE_LATS = np.full_like(ROUTE_LONS, 40.0)
ROUTE_MILES = float(geometry.cumulative_miles(ROUTE_LATS, ROUTE_LONS)[-1])


def fake_fetch_route(start_lat, start_lng, end_lat, end_lng, calls):
    calls.routing += 1
    return RouteData(
        distance_miles=ROUTE_MILES,
        duration_seconds=ROUTE_MILES / 60 * 3600,
        coordinates=[[x, y] for x, y in zip(ROUTE_LONS.tolist(), ROUTE_LATS.tolist())],
    )


def station(opis_id, lat, lng, price, name):
    return FuelStation(
        opis_id=opis_id, name=name, address="I-70 EXIT 1", city=name.title(), state="KS",
        price=price, latitude=lat, longitude=lng, geocode_source="place",
    )


@mock.patch("routing.services.planner.fetch_route", side_effect=fake_fetch_route)
class RouteApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Place.objects.bulk_create([
            Place(state="CO", name="Startville", key=place_key("Startville"), latitude=40.0, longitude=-104.0,
                  source="place"),
            Place(state="IL", name="Endtown", key=place_key("Endtown"), latitude=40.0, longitude=-90.0,
                  source="place"),
        ])
        FuelStation.objects.bulk_create([
            station(1, 40.02, -103.9, "3.80", "NEAR ORIGIN"),  # ~5 mi into the trip
            station(2, 40.00, -99.0, "3.00", "CHEAP MIDDLE"),  # ~265 mi
            station(3, 40.03, -95.0, "3.50", "LATER"),  # ~477 mi
            station(4, 45.00, -97.0, "1.00", "FAR OFF ROUTE"),  # 345 mi north: must be ignored
        ])

    def setUp(self):
        reset_station_index()
        bump_version()

    def get(self, **params):
        return self.client.get(reverse("route-plan"), params)

    def test_plan_structure_and_cost(self, fetch):
        response = self.get(start="Startville, CO", finish="Endtown, IL")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()

        self.assertEqual(data["start"]["source"], "gazetteer")
        self.assertAlmostEqual(data["route"]["distance_miles"], ROUTE_MILES, delta=0.1)
        self.assertEqual(data["route"]["geometry"]["type"], "LineString")
        self.assertIn("map_url", data)

        names = [s["name"] for s in data["fuel_stops"]]
        self.assertEqual(names, ["NEAR ORIGIN", "CHEAP MIDDLE"])
        self.assertNotIn("FAR OFF ROUTE", names)

        # Gallons bought cover the whole trip at 10 mpg, and costs add up.
        summary = data["summary"]
        self.assertAlmostEqual(summary["total_gallons_purchased"], ROUTE_MILES / 10, delta=0.05)
        self.assertAlmostEqual(summary["total_fuel_cost"], sum(s["cost"] for s in data["fuel_stops"]), delta=0.02)
        for stop in data["fuel_stops"]:
            self.assertAlmostEqual(stop["cost"], stop["gallons"] * stop["price_per_gallon"], delta=0.02)
            self.assertLessEqual(stop["fuel_on_departure_gallons"], 50)

        # First stop buys just enough to reach the cheap station; the cheap station covers the rest.
        cheap = data["fuel_stops"][1]
        self.assertAlmostEqual(cheap["gallons"], (ROUTE_MILES - cheap["mile_marker"]) / 10, delta=0.1)

    def test_one_routing_call_then_cached(self, fetch):
        first = self.get(start="Startville, CO", finish="Endtown, IL").json()
        self.assertEqual(first["meta"]["external_api_calls"], {"routing": 1, "geocoding": 0, "total": 1})
        self.assertFalse(first["meta"]["cached"])

        second = self.get(start="Startville, CO", finish="Endtown, IL").json()
        self.assertEqual(second["meta"]["external_api_calls"]["total"], 0)
        self.assertTrue(second["meta"]["cached"])
        self.assertEqual(second["summary"], first["summary"])

        # Different options reuse the cached route: still no external call.
        third = self.get(start="Startville, CO", finish="Endtown, IL", stop_penalty=0).json()
        self.assertEqual(third["meta"]["external_api_calls"]["total"], 0)
        self.assertEqual(fetch.call_count, 1)

    def test_post_with_coordinates_and_start_fuel(self, fetch):
        response = self.client.post(
            reverse("route-plan"),
            {"start": "40.0,-104.0", "finish": "40.0,-90.0", "start_fuel_gallons": 30},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["start"]["source"], "coordinates")
        self.assertAlmostEqual(data["summary"]["total_gallons_purchased"], ROUTE_MILES / 10 - 30, delta=0.05)
        self.assertNotIn("NEAR ORIGIN", [s["name"] for s in data["fuel_stops"]])

    def test_include_candidates(self, fetch):
        data = self.get(start="Startville, CO", finish="Endtown, IL", include_candidates="true").json()
        self.assertEqual({c["name"] for c in data["candidate_stations"]}, {"NEAR ORIGIN", "CHEAP MIDDLE", "LATER"})
        self.assertNotIn("candidate_stations", self.get(start="Startville, CO", finish="Endtown, IL").json())

    def test_validation_errors(self, fetch):
        self.assertEqual(self.get(start="Startville, CO").status_code, 400)
        self.assertEqual(self.get(start="A, CO", finish="B, IL", start_fuel_gallons=99).status_code, 400)
        self.assertEqual(self.get(start="A, CO", finish="B, IL", stop_penalty=-1).status_code, 400)

    def test_location_outside_usa(self, fetch):
        response = self.get(start="51.5,-0.12", finish="Endtown, IL")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_location")
        response = self.get(start="Toronto, ON", finish="Endtown, IL")
        self.assertEqual(response.status_code, 400)
        fetch.assert_not_called()

    def test_unfuelable_route(self, fetch):
        FuelStation.objects.filter(opis_id__in=[2, 3]).delete()
        bump_version()
        response = self.get(start="Startville, CO", finish="Endtown, IL")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "route_not_fuelable")

    def test_no_drivable_route(self, fetch):
        fetch.side_effect = NoRouteError("Impossible route between points")
        response = self.get(start="Startville, CO", finish="Endtown, IL")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "no_route")

    def test_map_page(self, fetch):
        response = self.client.get(reverse("route-map"), {"start": "Startville, CO", "finish": "Endtown, IL"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CHEAP MIDDLE")
        self.assertContains(response, 'id="plan-data"')
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)

    def test_health(self, fetch):
        self.assertEqual(self.client.get(reverse("health")).json(), {"status": "ok", "stations_loaded": 4})


class ImportCommandTests(TestCase):
    def test_load_fuel_stations_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            header = ["USPS", "GEOID", "ANSICODE", "NAME", "LSAD", "FUNCSTAT", "ALAND", "AWATER",
                      "ALAND_SQMI", "AWATER_SQMI", "INTPTLAT", "INTPTLONG"]
            places = [["OK", "1", "1", "Big Cabin town", "43", "A", "100", "0", "1", "0", "36.53", "-95.22"],
                      ["TN", "2", "2", "Nashville-Davidson metropolitan government (balance)", "00", "F",
                       "900", "0", "1", "0", "36.17", "-86.78"]]
            for name, rows in (("2023_Gaz_place_national.txt", places), ("2023_Gaz_cousubs_national.txt", [])):
                with open(tmp / name, "w", encoding="utf-8", newline="") as fh:
                    writer = csv.writer(fh, delimiter="\t")
                    writer.writerow(header)
                    writer.writerows(rows)
            fuel_csv = tmp / "prices.csv"
            fuel_csv.write_text(
                "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
                '7,WOODSHED OF BIG CABIN,"I-44, EXIT 283",Big Cabin,OK,307,3.00733333\n'
                '7,WOODSHED OF BIG CABIN,"I-44, EXIT 283",Big Cabin,OK,307,2.95\n'
                "8,PILOT,I-40,Nashville,TN,1,3.10\n"
                "9,UNKNOWN STOP,I-1,Nowhere,TX,1,3.10\n"
                "10,CANADA STOP,HWY 1,Calgary,AB,1,1.50\n",
                encoding="utf-8",
            )
            conf = {**settings.FUEL_ROUTE, "DATA_DIR": tmp}
            out = StringIO()
            with override_settings(FUEL_ROUTE=conf):
                call_command("load_fuel_stations", csv=str(fuel_csv), offline=True, stdout=out)

        stations = {s.opis_id: s for s in FuelStation.objects.all()}
        self.assertEqual(set(stations), {7, 8})  # unknown town skipped (offline), Canada skipped
        self.assertEqual(str(stations[7].price), "2.95000")  # lowest of the duplicate prices
        self.assertAlmostEqual(stations[8].latitude, 36.17)  # matched via consolidated-city alias
        self.assertIn("Could not geocode 1 stations", out.getvalue())


class PlaceSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        rows = [
            ("IL", "Chicago city", "Chicago", 0, 589_000_000),
            ("IL", "Chicago Heights city", "Chicago Heights", 0, 26_000_000),
            ("AK", "Chicken CDP", "Chicken", 1, 900_000_000),
            ("CA", "Chico city", "Chico", 0, 86_000_000),
            ("MO", "St. Louis city", "St. Louis", 0, 160_000_000),
        ]
        Place.objects.bulk_create(
            Place(state=st, name=name, key=place_key(name, gazetteer_name=True), display_name=display,
                  search_tier=tier, land_area=area, latitude=40 + i, longitude=-90 - i, source="place")
            for i, (st, name, display, tier, area) in enumerate(rows)
        )

    def labels(self, q, **extra):
        response = self.client.get(reverse("place-search"), {"q": q, **extra})
        self.assertEqual(response.status_code, 200)
        return [r["label"] for r in response.json()["results"]]

    def test_prefix_ranked_by_tier_then_size(self):
        # Incorporated cities beat a bigger census-designated place.
        self.assertEqual(self.labels("chic"), ["Chicago, IL", "Chico, CA", "Chicago Heights, IL", "Chicken, AK"])

    def test_state_filter(self):
        self.assertEqual(self.labels("chic, il"), ["Chicago, IL", "Chicago Heights, IL"])
        self.assertEqual(self.labels("chicago il"), ["Chicago, IL", "Chicago Heights, IL"])
        self.assertEqual(self.labels("chic, Calif"), ["Chico, CA"])
        self.assertEqual(self.labels("chic, ON"), [])

    def test_normalized_spelling_and_limits(self):
        self.assertEqual(self.labels("saint lou"), ["St. Louis, MO"])
        self.assertEqual(self.labels("c"), [])
        self.assertEqual(len(self.labels("chic", limit=2)), 2)

    def test_suggestion_label_resolves_without_external_call(self):
        from routing.services.external import CallLog
        from routing.services.geocoding import resolve_location

        calls = CallLog()
        for label in self.labels("chic"):
            self.assertEqual(resolve_location(label, calls).source, "gazetteer")
        self.assertEqual(calls.total, 0)
