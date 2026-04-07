"""Nautobot configuration for MNM."""

import os

from nautobot.core.settings import *  # noqa: F401,F403
from nautobot.core.settings import PLUGINS, PLUGINS_CONFIG

# ---------------------------------------------------------------------------
# Required settings
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ["NAUTOBOT_SECRET_KEY"]

ALLOWED_HOSTS = os.environ.get("NAUTOBOT_ALLOWED_HOSTS", "*").split(",")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASES = {
    "default": {
        "NAME": os.environ.get("NAUTOBOT_DB_NAME", "nautobot"),
        "USER": os.environ.get("NAUTOBOT_DB_USER", "nautobot"),
        "PASSWORD": os.environ.get("NAUTOBOT_DB_PASSWORD", ""),
        "HOST": os.environ.get("NAUTOBOT_DB_HOST", "postgres"),
        "PORT": os.environ.get("NAUTOBOT_DB_PORT", "5432"),
        "CONN_MAX_AGE": 300,
        "ENGINE": "django_prometheus.db.backends.postgresql",
    }
}

# ---------------------------------------------------------------------------
# Redis / Caching / Task queuing
# ---------------------------------------------------------------------------
_REDIS_HOST = os.environ.get("NAUTOBOT_REDIS_HOST", "redis")
_REDIS_PORT = os.environ.get("NAUTOBOT_REDIS_PORT", "6379")
_REDIS_PASSWORD = os.environ.get("NAUTOBOT_REDIS_PASSWORD", "")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"redis://:{_REDIS_PASSWORD}@{_REDIS_HOST}:{_REDIS_PORT}/0",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}

CELERY_BROKER_URL = f"redis://:{_REDIS_PASSWORD}@{_REDIS_HOST}:{_REDIS_PORT}/1"
CELERY_RESULT_BACKEND = f"redis://:{_REDIS_PASSWORD}@{_REDIS_HOST}:{_REDIS_PORT}/1"

# ---------------------------------------------------------------------------
# NAPALM (read-only network device access)
# ---------------------------------------------------------------------------
NAPALM_USERNAME = os.environ.get("NAUTOBOT_NAPALM_USERNAME", "")
NAPALM_PASSWORD = os.environ.get("NAUTOBOT_NAPALM_PASSWORD", "")
NAPALM_TIMEOUT = int(os.environ.get("NAUTOBOT_NAPALM_TIMEOUT", "30"))

# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------
PLUGINS.extend([
    "nautobot_plugin_nornir",
    "nautobot_ssot",
    "nautobot_device_onboarding",
    "welcome_wizard",
])

PLUGINS_CONFIG["nautobot_plugin_nornir"] = {
    "nornir_settings": {
        "credentials": "nautobot_plugin_nornir.plugins.credentials.env_vars.CredentialsEnvVars",
    },
    "connection_options": {
        "netmiko": {
            "extras": {
                # Override Netmiko's default 10s read_timeout for large Juniper configs.
                # This sets read_timeout_override on the connection, which Netmiko
                # uses for ALL send_command calls (ignoring per-call read_timeout).
                "read_timeout_override": 120,
                # Increase SSH connection timeout for slower devices (e.g., Juniper SRX series).
                "conn_timeout": 30,
            },
        },
    },
}

PLUGINS_CONFIG["nautobot_ssot"] = {}

PLUGINS_CONFIG["nautobot_device_onboarding"] = {
    "create_device_type_if_missing": True,
    "create_manufacturer_if_missing": True,
    "create_device_role_if_missing": True,
    "default_device_role": "Unknown",
    "default_device_status": "Active",
    "default_management_interface": "PLACEHOLDER",
    "default_management_prefix_length": 0,
}

PLUGINS_CONFIG["welcome_wizard"] = {
    "enable_devicetype-library": True,
    "enable_welcome_banner": False,
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("NAUTOBOT_LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "normal": {
            "format": "%(asctime)s %(name)s %(levelname)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "normal",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
}

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
PAGINATE_COUNT = 50
MAX_PAGE_SIZE = 1000

# Privacy-first: no phone-home (CLAUDE.md rule #3)
INSTALLATION_METRICS_ENABLED = False
