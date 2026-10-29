"""Listing import API: persistence, validation, auth boundaries."""

from __future__ import annotations

import json

import pytest
from django.test import Client

from listings.models import Listing

pytestmark = pytest.mark.django_db


def _post(client, payload, headers=None):
    return client.post(
        "/api/extension/listings",
        data=json.dumps(payload),
        content_type="application/json",
        headers=headers,
    )


def test_valid_import_persists_with_real_id(client, ext_headers, listing_payload):
    response = _post(client, listing_payload, ext_headers)
    assert response.status_code == 201, response.content
    body = response.json()
    listing = Listing.objects.get(pk=body["id"])  # ID must be a real row
    assert listing.source_url == listing_payload["source_url"]
    assert listing.advertised_claims == listing_payload["advertised_claims"]
    assert listing.import_status == "importing"
    assert body["photo_count"] == 0


def test_missing_vin_accepted(client, ext_headers, listing_payload):
    listing_payload["vin"] = None
    response = _post(client, listing_payload, ext_headers)
    assert response.status_code == 201
    assert Listing.objects.get(pk=response.json()["id"]).vin is None


def test_valid_vin_normalized(client, ext_headers, listing_payload):
    listing_payload["vin"] = "1hgcm82633a004352"
    response = _post(client, listing_payload, ext_headers)
    assert response.status_code == 201
    assert Listing.objects.get(pk=response.json()["id"]).vin == "1HGCM82633A004352"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(source_url="not-a-url"),
        lambda p: p.update(source_url="ftp://example.test/x"),
        lambda p: p.update(title=""),
        lambda p: p.update(source_site=""),
        lambda p: p.update(vin="SHORT"),
        lambda p: p.update(vin="1HGCM82633A00435I"),  # I forbidden
        lambda p: p.update(year=1500),
        lambda p: p.update(description="x" * 50_001),
        lambda p: p.update(advertised_claims=["x"] * 201),
        lambda p: p.update(expected_photo_count=41),
        lambda p: p.pop("title"),
    ],
)
def test_malformed_listing_rejected(client, ext_headers, listing_payload, mutate):
    mutate(listing_payload)
    response = _post(client, listing_payload, ext_headers)
    assert response.status_code in (400, 422), response.content
    assert Listing.objects.count() == 0


def test_missing_pairing_token_rejected(client, listing_payload):
    assert _post(client, listing_payload).status_code == 401
    assert _post(client, listing_payload, {}).status_code == 401


def test_invalid_pairing_token_rejected(client, listing_payload):
    response = _post(client, listing_payload, {"X-Extension-Token": "bogus"})
    assert response.status_code == 401
    assert Listing.objects.count() == 0


def test_extension_needs_no_csrf_token():
    """Header-authenticated endpoints work on a CSRF-enforcing client."""
    from pairing.services import create_pairing

    _, token = create_pairing(label="csrf-probe")
    client = Client(enforce_csrf_checks=True)
    response = client.post(
        "/api/extension/listings",
        data=json.dumps(
            {
                "source_url": "https://example.test/l/2",
                "source_site": "example.test",
                "title": "no csrf needed",
            }
        ),
        content_type="application/json",
        headers={"X-Extension-Token": token},
    )
    assert response.status_code == 201


def test_manual_create_matches_extension_validation(operator_client, listing_payload):
    from .conftest import post_json

    listing_payload["title"] = ""
    response = post_json(operator_client, "/api/listings", listing_payload)
    assert response.status_code == 422
    listing_payload["title"] = "manual import"
    response = post_json(operator_client, "/api/listings", listing_payload)
    assert response.status_code == 201, response.content
    assert Listing.objects.get(pk=response.json()["id"]).title == "manual import"


def test_mother_read_endpoints_need_session(client, ext_listing, operator_client):
    from django.test import Client as DjangoClient

    anon = DjangoClient(enforce_csrf_checks=True)  # operator_client shares `client`
    assert anon.get("/api/listings").status_code == 401
    assert anon.get(f"/api/listings/{ext_listing['id']}").status_code == 401
    response = operator_client.get("/api/listings")
    assert response.status_code == 200
    assert response.json()["count"] >= 1
    detail = operator_client.get(f"/api/listings/{ext_listing['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == ext_listing["id"]


def test_unknown_listing_is_404(operator_client):
    assert (
        operator_client.get(
            "/api/listings/00000000-0000-0000-0000-000000000000"
        ).status_code
        == 404
    )


def test_health_discloses_nothing_sensitive(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "secret" not in response.content.decode().lower()
    assert "token" not in response.content.decode().lower()
