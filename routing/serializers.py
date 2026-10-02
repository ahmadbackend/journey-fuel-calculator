from django.conf import settings
from rest_framework import serializers


def _tank_gallons():
    conf = settings.FUEL_ROUTE
    return conf["MAX_RANGE_MILES"] / conf["MILES_PER_GALLON"]


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        max_length=200,
        trim_whitespace=True,
        help_text="Start location in the USA: 'City, ST', a street address, or 'latitude,longitude'.",
    )
    finish = serializers.CharField(
        max_length=200,
        trim_whitespace=True,
        help_text="Finish location in the USA, same formats as start.",
    )
    start_fuel_gallons = serializers.FloatField(
        required=False,
        default=0.0,
        min_value=0.0,
        help_text="Fuel already in the tank at the start. 0 (default) means the trip starts by fueling up.",
    )
    stop_penalty = serializers.FloatField(
        required=False,
        default=None,
        allow_null=True,
        min_value=0.0,
        max_value=1000.0,
        help_text=(
            "Dollar weight of one fuel stop when choosing stops (default from settings, $5). "
            "0 returns the strictly cheapest plan however many stops it needs."
        ),
    )
    include_candidates = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Also return every station found along the route.",
    )

    def validate_start_fuel_gallons(self, value):
        tank = _tank_gallons()
        if value > tank:
            raise serializers.ValidationError(f"Cannot exceed the tank capacity of {tank:g} gallons.")
        return value
