"""Import the fuel price CSV into PostgreSQL with coordinates for every station.

Geocoding happens once, here, so the API never geocodes stations at request
time:

1. The Census place and county-subdivision gazetteers (vendored in data/) are
   loaded into the Place table and used to match each station's city/state.
2. The few towns not in the gazetteer are looked up once with Nominatim; the
   answers are stored in data/geocode_cache.json so re-imports are offline.

Usage:
    python manage.py load_fuel_stations [--csv PATH] [--offline]
"""

import csv
import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import FuelStation, Place
from routing.services.external import ExternalServiceError, geocode_nominatim
from routing.services.places import US_STATES, build_place_index, place_key
from routing.services.station_index import bump_version

PLACE_FILE = "2023_Gaz_place_national.txt"
COUSUB_FILE = "2023_Gaz_cousubs_national.txt"
GEOCODE_CACHE_FILE = "geocode_cache.json"


class Command(BaseCommand):
    help = "Load fuel stations from the CSV, geocode them, and store them in the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            default=str(settings.BASE_DIR / "fuel-prices-for-be-assessment.csv"),
            help="Path to the fuel price CSV.",
        )
        parser.add_argument(
            "--offline",
            action="store_true",
            help="Do not call Nominatim for towns missing from the gazetteer (they are skipped).",
        )

    def handle(self, *args, **opts):
        started = time.perf_counter()
        data_dir = Path(settings.FUEL_ROUTE["DATA_DIR"])
        csv_path = Path(opts["csv"])
        for path in (csv_path, data_dir / PLACE_FILE, data_dir / COUSUB_FILE):
            if not path.exists():
                raise CommandError(f"Missing file: {path}")

        self.stdout.write("Reading Census gazetteers...")
        index = build_place_index(
            [(data_dir / PLACE_FILE, Place.Source.PLACE), (data_dir / COUSUB_FILE, Place.Source.COUNTY_SUBDIVISION)]
        )
        self.stdout.write(f"  {len(index):,} unique place keys")

        stations, skipped_non_us, bad_rows = self._read_csv(csv_path)
        self.stdout.write(
            f"Read {len(stations):,} unique US stations from {csv_path.name} "
            f"(skipped {skipped_non_us:,} non-US rows, {bad_rows} invalid rows)"
        )

        cache_path = data_dir / GEOCODE_CACHE_FILE
        geo_cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        objects, unresolved = [], []
        counts = {source: 0 for source in FuelStation.GeocodeSource.values}

        for st in stations.values():
            entry = index.get((st["state"], place_key(st["city"])))
            if entry is not None:
                lat, lng, source = entry.latitude, entry.longitude, entry.source
            else:
                found = self._fallback_geocode(st["city"], st["state"], geo_cache, cache_path, opts["offline"])
                if not found:
                    unresolved.append(f"{st['city']}, {st['state']}")
                    continue
                lat, lng = found
                source = FuelStation.GeocodeSource.NOMINATIM
            counts[source] += 1
            objects.append(FuelStation(latitude=lat, longitude=lng, geocode_source=source, **st))

        with transaction.atomic():
            Place.objects.all().delete()
            Place.objects.bulk_create(
                (
                    Place(
                        state=e.state, name=e.name, key=e.key,
                        latitude=e.latitude, longitude=e.longitude, source=e.source,
                        display_name=e.display_name, land_area=e.land_area, search_tier=e.search_tier,
                    )
                    for e in index.values()
                ),
                batch_size=5000,
            )
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(objects, batch_size=2000)
        bump_version()

        self.stdout.write(
            self.style.SUCCESS(
                f"Stored {len(objects):,} stations "
                f"(place: {counts['place']:,}, county subdivision: {counts['cousub']:,}, "
                f"nominatim: {counts['nominatim']:,}) in {time.perf_counter() - started:.1f}s"
            )
        )
        if unresolved:
            self.stdout.write(
                self.style.WARNING(f"Could not geocode {len(unresolved)} stations: {sorted(set(unresolved))}")
            )

    def _read_csv(self, path):
        stations, skipped_non_us, bad_rows = {}, 0, 0
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                state = (row.get("State") or "").strip().upper()
                if state not in US_STATES:
                    skipped_non_us += 1
                    continue
                try:
                    opis_id = int(row["OPIS Truckstop ID"])
                    price = Decimal(row["Retail Price"].strip())
                    rack_id = int(row["Rack ID"]) if (row.get("Rack ID") or "").strip() else None
                except (KeyError, ValueError, InvalidOperation):
                    bad_rows += 1
                    continue
                if price <= 0:
                    bad_rows += 1
                    continue
                current = stations.get(opis_id)
                # Some stations are listed several times with different prices; keep the lowest.
                if current is None or price < current["price"]:
                    stations[opis_id] = {
                        "opis_id": opis_id,
                        "name": row["Truckstop Name"].strip(),
                        "address": row["Address"].strip(),
                        "city": " ".join(row["City"].split()),
                        "state": state,
                        "rack_id": rack_id,
                        "price": price.quantize(Decimal("0.00001")),
                    }
        return stations, skipped_non_us, bad_rows

    def _fallback_geocode(self, city, state, geo_cache, cache_path, offline):
        cache_key = f"{city}, {state}".lower()
        if cache_key in geo_cache:
            return geo_cache[cache_key]
        if offline:
            return None
        state_name = US_STATES[state]
        try:
            # Structured first (it will not confuse "Pecos" the town with Pecos County),
            # then free text for names OSM does not tag as a city.
            hit = geocode_nominatim(city=city, state=state_name) or geocode_nominatim(f"{city}, {state_name}, USA")
        except ExternalServiceError as exc:
            self.stderr.write(f"  Nominatim failed for {city}, {state}: {exc}")
            return None
        # Reject matches that landed in a different state.
        found = [hit["lat"], hit["lng"]] if hit and state_name in hit["label"] else None
        self.stdout.write(f"  Nominatim: {city}, {state} -> {found}")
        geo_cache[cache_key] = found
        cache_path.write_text(json.dumps(geo_cache, indent=1, sort_keys=True), encoding="utf-8")
        return found
