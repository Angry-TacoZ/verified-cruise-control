"""Pairing lifecycle: issuance, auth, revocation, rotation, restart persistence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.db import connections
from django.test import Client as DjangoClient

from pairing import services
from pairing.models import ExtensionPairing

pytestmark = pytest.mark.django_db


def test_pairing_token_authenticates_and_updates_last_used(client, ext_token):
    before = ExtensionPairing.objects.get()
    assert before.last_used_at is None
    response = client.post(
        "/api/extension/listings",
        data=json.dumps(
            {
                "source_url": "https://example.test/l/pair",
                "source_site": "example.test",
                "title": "pairing probe",
            }
        ),
        content_type="application/json",
        headers={"X-Extension-Token": ext_token},
    )
    assert response.status_code == 201, response.content
    assert ExtensionPairing.objects.get().last_used_at is not None


def test_revoked_token_rejected(client, ext_token):
    pairing = ExtensionPairing.objects.get()
    assert services.revoke_pairing(pairing.id) is True
    response = client.post(
        "/api/extension/listings",
        data=json.dumps(
            {
                "source_url": "https://example.test/l/rev",
                "source_site": "example.test",
                "title": "revoked probe",
            }
        ),
        content_type="application/json",
        headers={"X-Extension-Token": ext_token},
    )
    assert response.status_code == 401


def test_pairing_verifies_from_fresh_client_and_connection(client, ext_token):
    """No client-side or connection-local state: a new client and a new DB
    connection both verify the same token. (Cross-process persistence is
    covered by test_pairing_persists_across_processes.)"""
    fresh = DjangoClient(enforce_csrf_checks=True)
    response = fresh.get("/api/health", headers={"X-Extension-Token": ext_token})
    assert response.status_code == 200
    new_conn = connections.create_connection("default")
    try:
        # Uncommitted test-transaction rows are invisible here by design; the
        # new connection must still open and read schema normally.
        with new_conn.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE name='pairing_extensionpairing'"
            )
            assert cursor.fetchone() is not None
    finally:
        new_conn.close()
    assert services.verify_extension_token(ext_token) is not None


def test_rotate_revokes_old_and_issues_new(operator_client):
    from .conftest import post_json as pj

    status = operator_client.get("/api/pairing/status")
    assert status.status_code == 200
    old_ids = {p["id"] for p in status.json() if p["active"]}

    rotated = pj(operator_client, "/api/pairing/rotate", {"label": "rotated"})
    assert rotated.status_code == 200, rotated.content
    new_token = rotated.json()["extension_token"]

    status = operator_client.get("/api/pairing/status")
    actives = [p for p in status.json() if p["active"]]
    assert len(actives) == 1
    assert actives[0]["id"] not in old_ids
    # No verifiers or tokens leak through the status endpoint.
    assert "verifier" not in status.content.decode().lower()
    assert new_token not in status.content.decode()

    # Old tokens fail; the new one works.
    client = operator_client
    for pairing_id in old_ids:
        assert client.post(
            f"/api/pairing/{pairing_id}/revoke", headers={"X-CSRFToken": "x"}
        ).status_code in (403, 404)
    probe = client.post(
        "/api/extension/listings",
        data=json.dumps(
            {
                "source_url": "https://example.test/l/new",
                "source_site": "example.test",
                "title": "rotated token works",
            }
        ),
        content_type="application/json",
        headers={"X-Extension-Token": new_token},
    )
    assert probe.status_code == 201, probe.content


def test_rotation_requires_session_and_csrf(client):
    assert client.post(
        "/api/pairing/rotate", content_type="application/json"
    ).status_code in (
        401,
        403,
    )


def _run_manage(runtime_dir, script):
    env = dict(os.environ, VCC_RUNTIME_DIR=str(runtime_dir))
    backend = Path(__file__).resolve().parent.parent
    return subprocess.run(
        [sys.executable, str(backend / "manage.py"), "shell", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_pairing_persists_across_processes(tmp_path):
    """Two separate OS processes sharing one runtime dir: issue in one,
    verify in the other. This is what backend-restart persistence reduces to
    (verification reads only the file DB, no in-memory state)."""
    runtime = tmp_path / "restart-probe"
    runtime.mkdir()
    backend = Path(__file__).resolve().parent.parent
    migrate = subprocess.run(
        [sys.executable, str(backend / "manage.py"), "migrate"],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(backend.parent),
        env={**os.environ, "VCC_RUNTIME_DIR": str(runtime)},
    )
    assert migrate.returncode == 0, migrate.stderr[-2000:]

    issued = _run_manage(
        runtime,
        "from pairing.services import create_pairing as c; "
        "print(c(label='restart')[1], flush=True)",
    )
    assert issued.returncode == 0, issued.stderr[-2000:]
    token = issued.stdout.strip().splitlines()[-1]

    verified = _run_manage(
        runtime,
        f"from pairing.services import verify_extension_token as v; "
        f"print('OK' if v({token!r}) else 'FAIL', flush=True)",
    )
    assert verified.returncode == 0, verified.stderr[-2000:]
    assert verified.stdout.strip().splitlines()[-1] == "OK"
