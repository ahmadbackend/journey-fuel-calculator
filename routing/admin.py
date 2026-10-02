from django.contrib import admin

from .models import FuelStation, Place


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "price", "geocode_source")
    list_filter = ("state", "geocode_source")
    search_fields = ("name", "city", "opis_id")


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ("name", "state", "key", "latitude", "longitude")
    list_filter = ("state",)
    search_fields = ("name", "key")
