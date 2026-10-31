"""Django settings for Verified Cruise Control (localhost-first, PR #1)."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from .sqlite import register_sqlite_pragmas

BASE_DIR = Path(__file__).resolve().parent.parent

# All runtime state (SQLite, originals, secrets) lives here, outside tracked
# source. Overridable for tests and custom installs.
RUNTIME_DIR = Path(os.environ.get("VCC_RUNTIME_DIR", BASE_DIR / "runtime-data"))
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def _django_secret_key() -> str:
    """Environment secret, else a lazily-created untracked local secret file."""
    from_env = os.environ.get("DJANGO_SECRET_KEY")
    if from_env:
        return from_env
    secret_file = RUNTIME_DIR / ".django-secret"
    try:
        return secret_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        key = secrets.token_urlsafe(64)
        secret_file.write_text(key, encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return key


SECRET_KEY = _django_secret_key()
DEBUG = os.environ.get("VCC_DEBUG", "") == "1"

# Localhost only. Deliberately no CORS middleware: the mother app reaches
# Django through the Vite dev proxy (same origin), and the MV3 extension
# background worker uses host permissions, not CORS.
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
CSRF_TRUSTED_ORIGINS = ["http://127.0.0.1:5173", "http://localhost:5173"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "listings",
    "evidence",
    "pairing",
    "audits",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": RUNTIME_DIR / "vcc.sqlite3",
        "OPTIONS": {
            # Connect-level busy timeout (seconds)…
            "timeout": 5.0,
            # …plus exact per-connection PRAGMAs (see config/sqlite.py).
            "init_command": "PRAGMA synchronous = FULL; PRAGMA busy_timeout = 5000",
        },
        # File-backed test DB: WAL behavior cannot be proven on :memory:.
        "TEST": {"NAME": RUNTIME_DIR / "test-vcc.sqlite3"},
    }
}

register_sqlite_pragmas()

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = False  # plain-http localhost dev; no TLS terminator
CSRF_COOKIE_HTTPONLY = False  # JS must read it to send X-CSRFToken
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = False

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# --- PR #2 audit engine: inference enablement + credit safety ---
# Inference runs only when explicitly enabled AND an operator starts an
# audit. Malformed values fail closed at startup (no silent defaults).
AUDIT_INFERENCE_ENABLED = os.environ.get("VCC_INFERENCE_ENABLED", "") == "1"


def _audit_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}")
    if value <= 0:
        raise RuntimeError(f"{name} must be positive, got {raw!r}")
    return value


def _audit_decimal(name: str, default: str):
    from decimal import Decimal, InvalidOperation

    raw = os.environ.get(name, default)
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise RuntimeError(f"{name} must be a decimal, got {raw!r}")
    if value <= 0:
        raise RuntimeError(f"{name} must be positive, got {raw!r}")
    return value


AUDIT_MAX_CALLS_PER_JOB = _audit_int("VCC_AUDIT_MAX_CALLS", 60)
AUDIT_SPEND_CAP_USD = _audit_decimal("VCC_AUDIT_SPEND_CAP_USD", "2.00")
# Headroom quantum reserved before every provider call: a call proceeds
# only when spend + quantum fits under the cap. This bounds the *next*
# call's headroom; it is not a prediction of actual provider billing.
AUDIT_RESERVE_PER_CALL_USD = _audit_decimal("VCC_AUDIT_RESERVE_PER_CALL", "0.50")
AUDIT_MAX_CROPS_PER_JOB = _audit_int("VCC_AUDIT_MAX_CROPS", 12)
AUDIT_MAX_RECHECKS_PER_JOB = _audit_int("VCC_AUDIT_MAX_RECHECKS", 12)
AUDIT_WORKER_HEARTBEAT_S = _audit_int("VCC_AUDIT_HEARTBEAT_S", 300)
AUDIT_BASETEN_MODEL_ID = os.environ.get(
    "VCC_BASETEN_MODEL", "meta-models/muse-spark-open-1026"
)
AUDIT_SAM_MODEL_ID = os.environ.get("VCC_SAM_MODEL", "sam-3.1")
