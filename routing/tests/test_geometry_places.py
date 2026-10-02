import numpy as np
from django.test import SimpleTestCase

from routing.services import geometry
from routing.services.places import normalize_state, place_key, place_key_aliases


class GeometryTests(SimpleTestCase):
    def test_haversine_known_distance(self):
        # One degree along a meridian is 2*pi*R/360 miles.
        d = geometry.haversine_miles(40.0, -95.0, 41.0, -95.0)
        self.assertAlmostEqual(float(d), 2 * np.pi * geometry.EARTH_RADIUS_MILES / 360, places=6)

    def test_resample_spacing(self):
        lats = np.array([40.0, 40.0])
        lons = np.array([-100.0, -99.0])
        cum = geometry.cumulative_miles(lats, lons)
        _, _, miles = geometry.resample_polyline(lats, lons, cum, 1.0)
        self.assertEqual(miles[0], 0)
        self.assertAlmostEqual(miles[-1], cum[-1])
        self.assertTrue(np.allclose(np.diff(miles[:-1]), 1.0))

    def test_locate_points_along_route(self):
        lons = np.linspace(-100, -95, 500)
        lats = np.full_like(lons, 40.0)
        cum = geometry.cumulative_miles(lats, lons)
        s_lats, s_lons, s_miles = geometry.resample_polyline(lats, lons, cum, 1.0)
        # One point near the route at its middle, one 0.5 degrees (~35 mi) away.
        idx, miles, offsets = geometry.locate_points_along_route(
            s_lats, s_lons, s_miles, [40.05, 40.5], [-97.5, -97.5], max_offset_miles=10
        )
        self.assertEqual(idx.tolist(), [0])
        self.assertAlmostEqual(miles[0], cum[-1] / 2, delta=1.5)
        self.assertAlmostEqual(offsets[0], 3.45, delta=0.2)

    def test_simplify_keeps_shape_and_endpoints(self):
        lons = np.linspace(-100, -90, 1000)
        lats = 40 + np.where(lons < -95, 0.0, 1.0)  # straight, a step, straight
        keep = geometry.simplify_polyline(lats, lons)
        self.assertTrue(keep[0] and keep[-1])
        self.assertLess(keep.sum(), 10)


class PlaceNameTests(SimpleTestCase):
    def test_gazetteer_suffix_is_stripped_once(self):
        self.assertEqual(place_key("Abbeville city", gazetteer_name=True), "abbeville")
        self.assertEqual(place_key("Iowa City city", gazetteer_name=True), "iowacity")
        self.assertEqual(place_key("Iowa City"), "iowacity")

    def test_spelling_variants_share_a_key(self):
        self.assertEqual(place_key("Mc Calla"), place_key("McCalla CDP", gazetteer_name=True))
        self.assertEqual(place_key("De Forest"), place_key("DeForest village", gazetteer_name=True))
        self.assertEqual(place_key("Saint Louis"), place_key("St. Louis city", gazetteer_name=True))
        self.assertEqual(place_key("Fort Worth"), place_key("Ft. Worth"))

    def test_consolidated_city_alias(self):
        self.assertEqual(place_key_aliases("Nashville-Davidson metropolitan government (balance)"), ["nashville"])
        self.assertEqual(place_key_aliases("Dallas city"), [])

    def test_normalize_state(self):
        self.assertEqual(normalize_state("tx"), "TX")
        self.assertEqual(normalize_state("New Mexico"), "NM")
        self.assertIsNone(normalize_state("ON"))
