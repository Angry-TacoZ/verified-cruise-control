"""SQLite durability configuration.

Required operating point: ``journal_mode=WAL``, ``synchronous=FULL``,
``busy_timeout=5000``.

Verified against the installed Django 6.1 backend
(``django/db/backends/sqlite3/base.py``): ``OPTIONS["init_command"]`` is
supported — each ``;``-separated statement runs on every new connection —
so connection-level settings (``synchronous``, ``busy_timeout``) are
configured there in ``settings.py`` instead of a ``connection_created``
signal. ``OPTIONS["timeout"]`` additionally sets the sqlite3 connect-level
busy timeout.

WAL is persistent, not per-connection, so it is enabled once when the
database is prepared: the ``post_migrate`` hook below switches a file-backed
database into WAL mode idempotently (migrations run on every documented
setup path: dev scripts, tests, CI). No ``connection_created`` signal is
used.

Tests assert the *actual* PRAGMA values on a fresh file-backed database
(``backend/tests/test_sqlite.py``); configuration text alone proves nothing.
"""

from __future__ import annotations

from django.db import connection
from django.db.models.signals import post_migrate


def _enable_wal_once(sender, **kwargs) -> None:
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode;")
        row = cursor.fetchone()
        if row and str(row[0]).lower() == "wal":
            return
        cursor.execute("PRAGMA journal_mode=WAL;")


def register_sqlite_pragmas() -> None:
    post_migrate.connect(_enable_wal_once, dispatch_uid="vcc-sqlite-wal")
