"""SQLite durability: real PRAGMA values on file-backed databases."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _pragmas():
    with connection.cursor() as cursor:
        out = {}
        for name in ("journal_mode", "synchronous", "busy_timeout"):
            cursor.execute(f"PRAGMA {name};")
            out[name] = cursor.fetchone()[0]
    return out


def test_test_database_is_file_backed():
    assert connection.vendor == "sqlite"
    assert connection.settings_dict["NAME"] != ":memory:"


def test_required_pragmas_active():
    values = _pragmas()
    assert str(values["journal_mode"]).lower() == "wal"
    assert int(values["synchronous"]) == 2  # FULL
    assert int(values["busy_timeout"]) == 5000


def test_pragmas_on_fresh_connections():
    """Every new Django connection gets the PRAGMAs (init_command); WAL is
    file-level and visible from any connection."""
    from django.db import connections

    fresh = connections.create_connection("default")
    try:
        with fresh.cursor() as cursor:
            values = {}
            for name in ("journal_mode", "synchronous", "busy_timeout"):
                cursor.execute(f"PRAGMA {name};")
                values[name] = cursor.fetchone()[0]
    finally:
        fresh.close()
    assert str(values["journal_mode"]).lower() == "wal"
    assert int(values["synchronous"]) == 2
    assert int(values["busy_timeout"]) == 5000


def test_basic_concurrent_read_write():
    """A second connection can read while the test transaction is open."""
    from listings.models import Listing

    listing = Listing.objects.create(
        source_url="https://example.test/l/1",
        source_site="example.test",
        title="concurrency probe",
    )
    other = sqlite3.connect(str(connection.settings_dict["NAME"]), timeout=5.0)
    try:
        # Sees committed state (at minimum: the table exists and is readable).
        tables = other.execute(
            "SELECT name FROM sqlite_master WHERE name='listings_listing'"
        ).fetchall()
        assert tables
    finally:
        other.close()
    assert Listing.objects.filter(pk=listing.pk).exists()
