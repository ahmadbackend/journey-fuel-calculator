import random

from django.test import SimpleTestCase

from routing.services.optimizer import FuelPlanError, StationOnRoute, greedy_fuel_plan, plan_fuel_stops


def S(mile, price, name=None):
    return StationOnRoute(mile=mile, price=price, ref=name)


class OptimizerTests(SimpleTestCase):
    def test_hand_computed_plan(self):
        # A(0, $3) -> B(300, $2) -> C(600, $4) -> destination at 900.
        # Cheapest: buy 30 gal at A to reach B, fill 50 gal at B, top up 10 gal at C.
        stations = [S(0, 3.0, "A"), S(300, 2.0, "B"), S(600, 4.0, "C")]
        plan = plan_fuel_stops(stations, 900, stop_penalty=0)
        self.assertEqual([p.station.ref for p in plan.purchases], ["A", "B", "C"])
        self.assertEqual([round(p.gallons, 6) for p in plan.purchases], [30, 50, 10])
        self.assertAlmostEqual(plan.total_cost, 90 + 100 + 40)
        self.assertAlmostEqual(plan.total_gallons, 90)

    def test_short_trip_buys_only_what_is_needed(self):
        plan = plan_fuel_stops([S(0, 3.5), S(50, 3.0)], 120, stop_penalty=0)
        self.assertAlmostEqual(plan.total_gallons, 12)
        # 5 gal at $3.50 to reach the cheaper station, 7 gal at $3.00.
        self.assertAlmostEqual(plan.total_cost, 5 * 3.5 + 7 * 3.0)

    def test_skips_expensive_station_when_range_allows(self):
        plan = plan_fuel_stops([S(0, 3.0), S(200, 5.0)], 450, stop_penalty=0)
        self.assertEqual(len(plan.purchases), 1)
        self.assertAlmostEqual(plan.total_cost, 45 * 3.0)

    def test_start_fuel_reduces_purchases(self):
        plan = plan_fuel_stops([S(100, 3.0)], 400, start_fuel_miles=200)
        # Arrive at mile 100 with 100 miles of fuel, need 300 to finish -> buy 200 miles.
        self.assertAlmostEqual(plan.total_gallons, 20)
        self.assertAlmostEqual(plan.purchases[0].fuel_on_arrival_gallons, 10)

    def test_start_fuel_covers_whole_trip(self):
        plan = plan_fuel_stops([], 150, start_fuel_miles=200)
        self.assertEqual(plan.purchases, [])
        self.assertEqual(plan.total_cost, 0)

    def test_gap_longer_than_range_is_reported(self):
        with self.assertRaises(FuelPlanError) as ctx:
            plan_fuel_stops([S(0, 3.0), S(400, 3.0)], 1000)
        self.assertEqual(ctx.exception.gap_start_mile, 400)
        self.assertEqual(ctx.exception.gap_end_mile, 1000)

    def test_empty_tank_requires_station_at_origin(self):
        with self.assertRaises(FuelPlanError) as ctx:
            plan_fuel_stops([S(40, 3.0)], 300)
        self.assertEqual(ctx.exception.gap_start_mile, 0.0)

    def test_stop_penalty_trades_cents_for_fewer_stops(self):
        # Station B is a cent cheaper: worth a stop only when stops are free.
        stations = [S(0, 3.00), S(100, 2.99), S(200, 3.00)]
        free = plan_fuel_stops(stations, 450, stop_penalty=0)
        penalized = plan_fuel_stops(stations, 450, stop_penalty=5)
        self.assertEqual(len(free.purchases), 2)
        self.assertEqual(len(penalized.purchases), 1)
        self.assertLess(free.total_cost, penalized.total_cost)
        self.assertAlmostEqual(penalized.total_gallons, 45)

    def test_never_exceeds_tank_and_never_runs_dry(self):
        rng = random.Random(7)
        stations = [S(rng.uniform(0, 2000), rng.uniform(2.8, 4.2)) for _ in range(300)] + [S(0, 3.5)]
        plan = plan_fuel_stops(stations, 2000, stop_penalty=3)
        for p in plan.purchases:
            self.assertLessEqual(p.fuel_on_departure_gallons, 50 + 1e-6)
            self.assertGreaterEqual(p.fuel_on_arrival_gallons, -1e-6)
        self.assertAlmostEqual(plan.total_gallons, 200)

    def test_dp_matches_greedy_optimum_without_penalty(self):
        """The greedy is provably optimal for zero stop cost; the DP must agree."""
        rng = random.Random(1)
        checked = 0
        for _ in range(400):
            total = rng.uniform(50, 2500)
            max_range = rng.choice([150, 300, 500])
            stations = [S(rng.uniform(0, total), round(rng.uniform(2.5, 4.5), 3)) for _ in range(rng.randint(0, 50))]
            start_fuel = rng.choice([0, rng.uniform(0, max_range)])
            if start_fuel == 0:
                stations.append(S(0, round(rng.uniform(2.5, 4.5), 3)))
            try:
                greedy = greedy_fuel_plan(stations, total, max_range_miles=max_range, start_fuel_miles=start_fuel)
            except FuelPlanError:
                with self.assertRaises(FuelPlanError):
                    plan_fuel_stops(stations, total, max_range_miles=max_range, start_fuel_miles=start_fuel)
                continue
            dp = plan_fuel_stops(
                stations, total, max_range_miles=max_range, start_fuel_miles=start_fuel, stop_penalty=0
            )
            self.assertAlmostEqual(dp.total_cost, greedy.total_cost, places=4)
            self.assertLessEqual(len(dp.purchases), len(greedy.purchases))
            checked += 1
        self.assertGreater(checked, 100)
