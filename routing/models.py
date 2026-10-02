from django.db import models


class FuelStation(models.Model):
    """A truck stop from the fuel price file, geocoded to coordinates."""

    class GeocodeSource(models.TextChoices):
        PLACE = "place", "Census place gazetteer"
        COUNTY_SUBDIVISION = "cousub", "Census county subdivision gazetteer"
        NOMINATIM = "nominatim", "Nominatim (one-time import fallback)"

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    # Lowest retail price listed for this station in the source file.
    price = models.DecimalField(max_digits=8, decimal_places=5)
    latitude = models.FloatField()
    longitude = models.FloatField()
    geocode_source = models.CharField(max_length=16, choices=GeocodeSource.choices)

    class Meta:
        ordering = ["opis_id"]
        indexes = [models.Index(fields=["latitude", "longitude"])]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"


class Place(models.Model):
    """US place from the Census gazetteer, used for local geocoding.

    One row per (state, normalized name). When several gazetteer entries share
    a key, incorporated places/CDPs beat county subdivisions and the larger
    land area wins.
    """

    class Source(models.TextChoices):
        PLACE = "place", "Census place"
        COUNTY_SUBDIVISION = "cousub", "Census county subdivision"

    state = models.CharField(max_length=2)
    name = models.CharField(max_length=128)
    # Normalized lookup key (see routing.services.places.place_key).
    key = models.CharField(max_length=128)
    latitude = models.FloatField()
    longitude = models.FloatField()
    source = models.CharField(max_length=8, choices=Source.choices)
    # Name without the Census suffix, e.g. "Iowa City" for "Iowa City city".
    display_name = models.CharField(max_length=128, default="")
    # Autocomplete ranking: 0 incorporated place, 1 census-designated place,
    # 2 county subdivision; then bigger land area (square meters) first.
    search_tier = models.PositiveSmallIntegerField(default=0)
    land_area = models.BigIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["state", "key"], name="unique_place_state_key")]
        indexes = [
            # Prefix searches (key LIKE 'chic%') for autocomplete.
            models.Index(fields=["key"], name="place_key_prefix_idx", opclasses=["varchar_pattern_ops"]),
        ]

    def __str__(self):
        return f"{self.display_name or self.name}, {self.state}"
