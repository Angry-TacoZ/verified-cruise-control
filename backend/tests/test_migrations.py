"""Clean-checkout migration: `manage.py migrate` on an empty runtime dir."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def test_migrate_from_scratch(tmp_path):
    runtime = tmp_path / "fresh"
    runtime.mkdir()
    proc = subprocess.run(
        [sys.executable, str(BACKEND_DIR / "manage.py"), "migrate"],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(BACKEND_DIR.parent),
        env={**os.environ, "VCC_RUNTIME_DIR": str(runtime)},
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    db = runtime / "vcc.sqlite3"
    assert db.is_file()
    conn = sqlite3.connect(str(db))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "listings_listing",
            "evidence_photograph",
            "pairing_extensionpairing",
            "pairing_localoperator",
        } <= tables
        assert conn.execute("PRAGMA journal_mode;").fetchone()[0].lower() == "wal"
    finally:
        conn.close()
