"""Superset configuration for Nova. Every secret comes from the environment."""

import os
from urllib.parse import urlsplit

from cachelib.redis import RedisCache


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set; run `make env` to generate .env")
    return value


SECRET_KEY = _require("SUPERSET_SECRET_KEY")
SQLALCHEMY_DATABASE_URI = _require("SUPERSET_DATABASE_URI")

# The browser origin of the Nova UI: the only site allowed to frame dashboards.
UI_ORIGIN = _require("NOVA_UI_ORIGIN").rstrip("/")

# --- Redis ------------------------------------------------------------------
# REDIS_URL carries the host; each concern gets its own logical database.
_redis = urlsplit(_require("REDIS_URL"))
_REDIS_HOST = _redis.hostname or "redis"
_REDIS_PORT = _redis.port or 6379
_REDIS_BASE = f"redis://{_REDIS_HOST}:{_REDIS_PORT}"


def _cache(prefix: str, db: int, timeout: int = 300) -> dict:
    return {
        "CACHE_TYPE": "RedisCache",
        "CACHE_DEFAULT_TIMEOUT": timeout,
        "CACHE_KEY_PREFIX": prefix,
        "CACHE_REDIS_URL": f"{_REDIS_BASE}/{db}",
    }


CACHE_CONFIG = _cache("superset_cache_", 1)
DATA_CACHE_CONFIG = _cache("superset_data_", 2, timeout=600)
FILTER_STATE_CACHE_CONFIG = _cache("superset_filter_", 3, timeout=86400)
EXPLORE_FORM_DATA_CACHE_CONFIG = _cache("superset_explore_", 3, timeout=86400)
RESULTS_BACKEND = RedisCache(
    host=_REDIS_HOST, port=_REDIS_PORT, db=4, key_prefix="superset_results_"
)
# The image runs with SUPERSET_ENV=production, which turns the rate limiter on.
# It needs a shared store or every gunicorn worker counts separately.
RATELIMIT_STORAGE_URI = f"{_REDIS_BASE}/5"


class CeleryConfig:
    broker_url = f"{_REDIS_BASE}/6"
    result_backend = f"{_REDIS_BASE}/7"
    imports = ("superset.sql_lab", "superset.tasks.scheduler")
    worker_prefetch_multiplier = 1
    task_acks_late = False


CELERY_CONFIG = CeleryConfig

# --- Embedding and guest tokens ----------------------------------------------
FEATURE_FLAGS = {"EMBEDDED_SUPERSET": True}

GUEST_TOKEN_JWT_SECRET = _require("SUPERSET_GUEST_TOKEN_SECRET")
GUEST_TOKEN_JWT_EXP_SECONDS = 600
# Gamma has no datasource access of its own; guests only reach what their token
# names, unlike the default "Public" role.
GUEST_ROLE_NAME = "Gamma"

# --- CORS and CSP ------------------------------------------------------------
ENABLE_CORS = True
CORS_OPTIONS = {
    "origins": [UI_ORIGIN],
    "supports_credentials": True,
    "allow_headers": ["*"],
    "resources": ["/api/*"],
}

TALISMAN_ENABLED = True
TALISMAN_CONFIG = {
    # X-Frame-Options has no way to name an allowed origin, so framing is
    # controlled by the frame-ancestors directive below instead.
    "frame_options": None,
    "content_security_policy": {
        "base-uri": ["'self'"],
        "default-src": ["'self'"],
        "frame-ancestors": ["'self'", UI_ORIGIN],
        "img-src": ["'self'", "blob:", "data:"],
        "worker-src": ["'self'", "blob:"],
        "connect-src": ["'self'"],
        "object-src": "'none'",
        "style-src": ["'self'", "'unsafe-inline'"],
        "font-src": ["'self'"],
        "script-src": ["'self'", "'strict-dynamic'"],
    },
    "content_security_policy_nonce_in": ["script-src"],
    "force_https": False,
    "session_cookie_secure": False,
}

# --- Look and feel -----------------------------------------------------------
# The categorical palette is validated for colour blindness on Nova's surfaces, and is the
# same one Nova's live previews use, so a chart looks the same when the dashboard takes over.
EXTRA_CATEGORICAL_COLOR_SCHEMES = [{
    "id": "novaColors",
    "label": "Nova",
    "description": "Validated for colour blindness; order matters",
    "isDefault": True,
    "colors": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
}]

# Setting a theme replaces Superset's defaults outright, so its other tokens are repeated here.
_BRAND_TOKENS = {
    "brandAppName": "Nova",
    "brandLogoAlt": "Nova",
    "brandLogoUrl": "/static/assets/images/superset-logo-horiz.png",
    "brandLogoMargin": "18px 0",
    "brandLogoHref": "/",
    "brandLogoHeight": "24px",
    "brandSpinnerUrl": None,
    "brandSpinnerSvg": None,
    "brandIconMaxWidth": 37,
    "colorSuccess": "#16a34a",
    "colorWarning": "#d97706",
    "colorError": "#dc2626",
    "colorInfo": "#2a78d6",
    "fontUrls": [],
    "fontFamily": "Inter, Helvetica, Arial, sans-serif",
    "fontFamilyCode": "'IBM Plex Mono', 'Courier New', monospace",
    "fontSizeXS": "8",
    "fontSizeXXL": "28",
    "fontWeightNormal": "400",
    "fontWeightLight": "300",
    "fontWeightStrong": "500",
    "fontWeightBold": "700",
    "transitionTiming": 0.3,
    "colorEditorSelection": "#fff5cf",
    "borderRadius": 8,
}
THEME_DEFAULT = {"token": {**_BRAND_TOKENS, "colorPrimary": "#4f46e5", "colorLink": "#4f46e5"}, "algorithm": "default"}
THEME_DARK = {"token": {**_BRAND_TOKENS, "colorPrimary": "#818cf8", "colorLink": "#818cf8",
                        "colorEditorSelection": "#5c4d1a"}, "algorithm": "dark"}

# --- Timeouts ----------------------------------------------------------------
# Trino queries over JDBC catalogs can outlast Superset's 60s defaults. Keep
# GUNICORN_TIMEOUT in docker-compose.yml in step with these.
SUPERSET_WEBSERVER_TIMEOUT = 120
SQLLAB_TIMEOUT = 120
