"""Shared fixtures: full-stack Django clients, tokens, synthetic images."""

from __future__ import annotations

import io
import json
import struct
import zlib

import pytest
from django.test import Client
from PIL import Image, ImageDraw

from pairing import services as pairing_services


@pytest.fixture(autouse=True)
def _isolated_runtime_dir(tmp_path, settings):
    """Every test gets a fresh filesystem runtime dir (DB stays test-wide)."""
    runtime = tmp_path / "runtime-data"
    runtime.mkdir()
    settings.RUNTIME_DIR = runtime
    return runtime


@pytest.fixture()
def client():
    return Client(enforce_csrf_checks=True)


@pytest.fixture()
def ext_token():
    _, token = pairing_services.create_pairing(label="test")
    return token


@pytest.fixture()
def ext_headers(ext_token):
    return {"X-Extension-Token": ext_token}


@pytest.fixture()
def listing_payload():
    return {
        "source_url": "https://example-motors.test/listings/1987-test-coupe",
        "source_site": "example-motors.test",
        "year": 1987,
        "make": "Test",
        "model": "Coupe",
        "trim": "LX",
        "vin": None,
        "title": "1987 Test Coupe LX — synthetic fixture",
        "description": "Synthetic listing used for automated tests.",
        "advertised_claims": ["cruise control", "lane keeping assist"],
        "expected_photo_count": 2,
    }


@pytest.fixture()
def ext_listing(client, ext_headers, listing_payload):
    response = client.post(
        "/api/extension/listings",
        data=json.dumps(listing_payload),
        content_type="application/json",
        headers=ext_headers,
    )
    assert response.status_code == 201, response.content
    return response.json()


def _draw_test_card(draw: ImageDraw.ImageDraw, label: str) -> None:
    draw.rectangle([4, 4, 60, 44], outline="white", width=2)
    draw.ellipse([14, 12, 50, 36], fill="gray", outline="white")
    draw.text((8, 34), label, fill="white")


def make_image_bytes(fmt: str, label: str = "SYN") -> bytes:
    img = Image.new("RGB", (64, 48), color=(30, 60, 120))
    _draw_test_card(ImageDraw.Draw(img), label)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture()
def png_bytes():
    return make_image_bytes("PNG", "PNG")


@pytest.fixture()
def jpeg_bytes():
    return make_image_bytes("JPEG", "JPG")


@pytest.fixture()
def webp_bytes():
    return make_image_bytes("WEBP", "WBP")


def make_png_with_dimensions(width: int, height: int) -> bytes:
    """Minimal PNG with a chosen IHDR size and valid CRCs (pixel data absent)."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return sig + struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk))


@pytest.fixture()
def csrf_client(client):
    """Client that fetched a CSRF cookie; helper posts JSON with the token."""

    response = client.get("/api/csrf")
    assert response.status_code == 200
    token = response.json()["csrf_token"]
    client.csrf_token = token  # type: ignore[attr-defined]
    return client


def post_json(client, path, payload, *, csrf: bool = True):
    headers = {}
    if csrf and hasattr(client, "csrf_token"):
        headers["X-CSRFToken"] = client.csrf_token
    return client.post(
        path,
        data=json.dumps(payload),
        content_type="application/json",
        headers=headers or None,
    )


@pytest.fixture()
def operator_client(csrf_client):
    """Client with a completed operator bootstrap (session authenticated)."""
    from pairing.services import ensure_setup_token

    token = ensure_setup_token()
    assert token is not None
    response = post_json(csrf_client, "/api/setup/initialize", {"setup_token": token})
    assert response.status_code == 200, response.content
    return csrf_client
