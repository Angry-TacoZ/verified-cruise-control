"""Zero-inference guarantee: the whole import path works with no model keys
and cannot reach the network."""

from __future__ import annotations

import json
import socket

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from evidence.models import Photograph
from listings.models import Listing

from .conftest import make_image_bytes

pytestmark = pytest.mark.django_db


def _block_sockets(monkeypatch):
    def _denied(*args, **kwargs):
        raise AssertionError("network access attempted during import")

    monkeypatch.setattr(socket, "socket", _denied)
    monkeypatch.setattr(socket, "create_connection", _denied)


def test_import_flow_makes_zero_network_calls(
    client, ext_headers, listing_payload, monkeypatch, png_bytes
):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)
    _block_sockets(monkeypatch)

    created = client.post(
        "/api/extension/listings",
        data=json.dumps(listing_payload),
        content_type="application/json",
        headers=ext_headers,
    )
    assert created.status_code == 201, created.content
    listing_id = created.json()["id"]

    for label in ("NET-A", "NET-B"):
        uploaded = client.post(
            f"/api/extension/listings/{listing_id}/photos",
            data={
                "file": SimpleUploadedFile(
                    f"{label}.png", make_image_bytes("PNG", label), "image/png"
                )
            },
            headers=ext_headers,
        )
        assert uploaded.status_code == 201, uploaded.content

    assert Listing.objects.count() == 1
    assert Photograph.objects.count() == 2
    assert Listing.objects.get().import_status == "complete"


def test_missing_credentials_do_not_break_setup_or_reads(
    client, csrf_client, monkeypatch
):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)
    assert client.get("/api/health").status_code == 200
    assert csrf_client.get("/api/setup/status").status_code == 200
