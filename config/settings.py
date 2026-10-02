"""
Django settings for the fuel route optimizer project.

Secrets and environment-specific values are read from the `.env` file in the
project root (see `.env.example`).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-key-change-me-in-production-0123456789",
)
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "routing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database: PostgreSQL. Credentials come from .env.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DATABASE_NAME", "fuel_route"),
        "USER": os.getenv("DATABASE_USERNAME", "postgres"),
        "PASSWORD": os.getenv("DATABASE_PASSWORD", ""),
        "HOST": os.getenv("DATABASE_HOST", "localhost"),
        "PORT": os.getenv("DATABASE_PORT", "5432"),
        # psycopg connection pool (Django 5.1+): requests reuse open connections
        # instead of paying for a new PostgreSQL login each time.
        "OPTIONS": {"pool": {"min_size": 2, "max_size": 10, "timeout": 10}},
    }
}

# Route results and routing-API responses are cached in PostgreSQL so they are
# shared between worker processes and survive restarts. This keeps calls to the
# free routing API to a minimum: a repeated query costs zero external calls.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "route_cache",
        "TIMEOUT": int(os.getenv("ROUTE_CACHE_SECONDS", 60 * 60 * 24)),
        "OPTIONS": {"MAX_ENTRIES": 20000},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Django's default ("same-origin") sends no Referer to tile servers, which
# tile usage policies require.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"routing": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")}},
}


# --- Fuel route settings ----------------------------------------------------
FUEL_ROUTE = {
    # Vehicle assumptions from the assignment.
    "MAX_RANGE_MILES": 500.0,
    "MILES_PER_GALLON": 10.0,
    # A station counts as "on the route" when it is within this many miles of
    # the route line. Station coordinates are city centroids, so this needs to
    # be a few miles wider than the true distance from the highway.
    "CORRIDOR_MILES": float(os.getenv("CORRIDOR_MILES", 10)),
    # With an empty tank the trip starts by fueling at a station this close to
    # the origin (measured along the route).
    "ORIGIN_FUEL_RADIUS_MILES": float(os.getenv("ORIGIN_FUEL_RADIUS_MILES", 30)),
    # Default dollar weight of one fuel stop in the optimizer (time/overhead).
    # 0 = strictly cheapest fuel, regardless of how many stops it takes.
    "STOP_PENALTY_DOLLARS": float(os.getenv("STOP_PENALTY_DOLLARS", 5)),
    # Free routing API (OSRM public demo server, no key needed).
    "OSRM_BASE_URL": os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org"),
    # Free geocoder used only when a location cannot be resolved locally.
    "NOMINATIM_BASE_URL": os.getenv("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org"),
    # Map tiles for the /map/ page (loaded by the browser, not the server).
    # OpenStreetMap's own tile servers block apps like this one ("Access
    # blocked" tiles), so the default is Esri's free World Street Map.
    "MAP_TILE_URL": os.getenv(
        "MAP_TILE_URL",
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    ),
    "MAP_TILE_ATTRIBUTION": os.getenv(
        "MAP_TILE_ATTRIBUTION",
        "Tiles &copy; Esri &mdash; Sources: Esri, HERE, Garmin, USGS, NGA, EPA, USDA, NPS &middot; "
        "Route &copy; OpenStreetMap contributors (OSRM)",
    ),
    "HTTP_USER_AGENT": os.getenv("HTTP_USER_AGENT", "fuel-route-optimizer/1.0"),
    "HTTP_TIMEOUT_SECONDS": float(os.getenv("HTTP_TIMEOUT_SECONDS", 20)),
    "DATA_DIR": BASE_DIR / "data",
}
