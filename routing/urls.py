from django.urls import path

from . import views

urlpatterns = [
    path("api/route/", views.RoutePlanView.as_view(), name="route-plan"),
    path("api/places/", views.PlaceSearchView.as_view(), name="place-search"),
    path("api/health/", views.HealthView.as_view(), name="health"),
    path("map/", views.route_map, name="route-map"),
    path("", views.route_map, name="home"),
]
