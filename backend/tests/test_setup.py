"""Operator bootstrap: setup status, one-time token, session, logout."""

from __future__ import annotations

import pytest

from pairing import services
from pairing.models import LocalOperator

from .conftest import post_json

pytestmark = pytest.mark.django_db


def test_setup_status_uninitialized_initially(csrf_client):
    assert csrf_client.get("/api/setup/status").json() == {"initialized": False}


def test_initialize_requires_csrf_token(client):
    token = services.ensure_setup_token()
    assert token is not None
    # No CSRF cookie/token at all.
    response = client.post(
        "/api/setup/initialize",
        data={"setup_token": token},
        content_type="application/json",
    )
    assert response.status_code == 403
    assert services.get_operator().initialized is False


def test_initialize_rejects_bad_token(csrf_client):
    response = post_json(csrf_client, "/api/setup/initialize", {"setup_token": "nope"})
    assert response.status_code == 403
    assert services.get_operator().initialized is False


def test_initialize_once_then_token_dead(csrf_client):
    token = services.ensure_setup_token()
    first = post_json(csrf_client, "/api/setup/initialize", {"setup_token": token})
    assert first.status_code == 200, first.content
    assert first.json()["extension_token"]
    assert first.json()["fresh"] is True
    assert services.ensure_setup_token() is None  # no implicit second issuance
    assert services.get_operator().initialized is True
    # Replaying the consumed setup token fails.
    replay = post_json(csrf_client, "/api/setup/initialize", {"setup_token": token})
    assert replay.status_code == 403


def test_reentry_opens_session_without_new_pairing(csrf_client):
    from django.test import Client as DjangoClient

    from pairing.models import ExtensionPairing

    bootstrap = services.ensure_setup_token()
    assert (
        post_json(
            csrf_client, "/api/setup/initialize", {"setup_token": bootstrap}
        ).status_code
        == 200
    )
    assert ExtensionPairing.objects.count() == 1

    # A second browser (fresh client, no session) re-enters explicitly.
    second = DjangoClient(enforce_csrf_checks=True)
    reentry = services.ensure_setup_token(regenerate=True)
    assert reentry is not None and reentry != bootstrap
    csrf = second.get("/api/csrf").json()["csrf_token"]
    response = second.post(
        "/api/setup/initialize",
        data={"setup_token": reentry},
        content_type="application/json",
        headers={"X-CSRFToken": csrf},
    )
    assert response.status_code == 200, response.content
    assert response.json()["extension_token"] is None
    assert response.json()["fresh"] is False
    assert ExtensionPairing.objects.count() == 1  # no pairing minted
    assert second.get("/api/listings").status_code == 200  # session works
    # The re-entry token was single-use too.
    assert services.verify_setup_token(reentry) is False


def test_setup_token_single_use_without_recovery(csrf_client):
    assert services.ensure_setup_token() is not None
    # Already issued and unrecoverable: must explicitly regenerate.
    assert services.ensure_setup_token() is None
    assert services.ensure_setup_token(regenerate=True) is not None


def test_logout_clears_session(operator_client):
    assert operator_client.get("/api/listings").status_code == 200
    response = post_json(operator_client, "/api/setup/logout", {})
    assert response.status_code == 200
    assert operator_client.get("/api/listings").status_code == 401


def test_operator_row_is_singleton():
    services.get_operator()
    services.get_operator()
    assert LocalOperator.objects.count() == 1
