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
