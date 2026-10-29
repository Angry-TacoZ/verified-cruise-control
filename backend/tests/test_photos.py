"""Photo upload/storage: validation, idempotency, limits, access control."""

from __future__ import annotations

import hashlib
import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from config import limits
from evidence import images
from evidence.models import Photograph
from listings.models import Listing

from .conftest import make_image_bytes, make_png_with_dimensions, post_json

pytestmark = pytest.mark.django_db


def _upload(client, headers, listing_id, data, name="photo.png", ctype="image/png"):
    return client.post(
        f"/api/extension/listings/{listing_id}/photos",
        data={"file": SimpleUploadedFile(name, data, content_type=ctype)},
        headers=headers,
    )


def test_valid_photo_saved_with_hash(client, ext_headers, ext_listing, png_bytes):
    response = _upload(client, ext_headers, ext_listing["id"], png_bytes)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["sha256"] == hashlib.sha256(png_bytes).hexdigest()
    assert body["duplicate"] is False
    photo = Photograph.objects.get(pk=body["id"])
    assert photo.width == 64 and photo.height == 48
    assert photo.image_format == "PNG" and photo.mime_type == "image/png"
    stored = images.original_path(photo.storage_ref, photo.image_format)
    assert stored.is_file()
    assert stored.read_bytes() == png_bytes  # original bytes preserved


@pytest.mark.parametrize(
    "fmt,label", [("JPEG", "JPG"), ("PNG", "PNG"), ("WEBP", "WBP")]
)
def test_supported_formats(client, ext_headers, ext_listing, fmt, label):
    data = make_image_bytes(fmt, label)
    response = _upload(
        client, ext_headers, ext_listing["id"], data, name=f"x.{fmt.lower()}"
    )
    assert response.status_code == 201, response.content
    assert response.json()["image_format"] == fmt


def test_duplicate_upload_is_idempotent(client, ext_headers, ext_listing, png_bytes):
    first = _upload(client, ext_headers, ext_listing["id"], png_bytes)
    second = _upload(
        client, ext_headers, ext_listing["id"], png_bytes, name="other.png"
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["duplicate"] is True
    assert Photograph.objects.count() == 1


def test_same_bytes_distinct_listings_stay_distinct(
    client, ext_headers, ext_listing, listing_payload, png_bytes
):
    listing_payload["source_url"] = "https://example-motors.test/listings/other"
    created = client.post(
        "/api/extension/listings",
        data=json.dumps(listing_payload),
        content_type="application/json",
        headers=ext_headers,
    )
    other_id = created.json()["id"]
    assert other_id != ext_listing["id"]
    assert _upload(client, ext_headers, ext_listing["id"], png_bytes).status_code == 201
    response = _upload(client, ext_headers, other_id, png_bytes)
    assert response.status_code == 201
    assert response.json()["duplicate"] is False
    assert Photograph.objects.count() == 2


def test_unsupported_type_rejected(client, ext_headers, ext_listing):
    response = _upload(
        client,
        ext_headers,
        ext_listing["id"],
        b"%PDF-1.4 fake",
        name="x.pdf",
        ctype="application/pdf",
    )
    assert response.status_code == 422, response.content
    assert Photograph.objects.count() == 0


def test_mime_spoof_rejected(client, ext_headers, ext_listing):
    response = _upload(
        client,
        ext_headers,
        ext_listing["id"],
        b"not an image",
        name="x.png",
        ctype="image/png",
    )
    assert response.status_code == 422
    assert Photograph.objects.count() == 0


def test_wrong_extension_ok_when_bytes_valid(
    client, ext_headers, ext_listing, jpeg_bytes
):
    """Extensions are untrusted: valid JPEG bytes named .png still store as JPEG."""
    response = _upload(client, ext_headers, ext_listing["id"], jpeg_bytes, name="x.png")
    assert response.status_code == 201, response.content
    assert response.json()["image_format"] == "JPEG"


def test_oversized_file_rejected(client, ext_headers, ext_listing, png_bytes):
    big = png_bytes + b"\0" * (limits.MAX_PHOTO_BYTES + 1)
    response = _upload(client, ext_headers, ext_listing["id"], big)
    assert response.status_code == 413, response.content
    assert Photograph.objects.count() == 0


def test_excessive_dimensions_rejected_before_decode(ext_listing):
    data = make_png_with_dimensions(10_000, 5_000)  # 50 MP header, no pixels
    with pytest.raises(images.ImageRejected) as exc:
        images.validate_image_bytes(data)
    assert exc.value.code in ("dimensions", "unreadable")
    assert Photograph.objects.count() == 0


def test_decompression_bomb_warning_is_error(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(images.ImageRejected):
        images.validate_image_bytes(make_image_bytes("PNG"))


def test_truncated_image_rejected(client, ext_headers, ext_listing, jpeg_bytes):
    response = _upload(
        client, ext_headers, ext_listing["id"], jpeg_bytes[: len(jpeg_bytes) // 2]
    )
    assert response.status_code == 422, response.content
    assert Photograph.objects.count() == 0


def test_path_traversal_filename_never_used_as_path(
    client, ext_headers, ext_listing, png_bytes
):
    response = _upload(
        client, ext_headers, ext_listing["id"], png_bytes, name="../../evil.png"
    )
    assert response.status_code == 201, response.content
    photo = Photograph.objects.get(pk=response.json()["id"])
    stored = images.original_path(photo.storage_ref, photo.image_format)
    assert stored.parent.name == "originals"
    assert ".." not in str(stored)
    assert photo.original_filename == "evil.png"


def test_max_photos_per_listing_enforced(client, ext_headers, ext_listing, monkeypatch):
    monkeypatch.setattr(limits, "MAX_PHOTOS_PER_LISTING", 1)
    assert (
        _upload(
            client, ext_headers, ext_listing["id"], make_image_bytes("PNG", "A")
        ).status_code
        == 201
    )
    response = _upload(
        client, ext_headers, ext_listing["id"], make_image_bytes("PNG", "B")
    )
    assert response.status_code == 422
    assert Photograph.objects.count() == 1


def test_failed_uploads_mark_listing_partial(
    client, ext_headers, ext_listing, png_bytes
):
    assert _upload(client, ext_headers, ext_listing["id"], png_bytes).status_code == 201
    assert _upload(client, ext_headers, ext_listing["id"], b"junk").status_code == 422
    listing = Listing.objects.get(pk=ext_listing["id"])
    assert listing.failed_upload_count == 1
    assert listing.import_status == "partial"


def test_complete_status_when_expected_met(client, ext_headers, ext_listing):
    assert (
        _upload(
            client, ext_headers, ext_listing["id"], make_image_bytes("PNG", "A")
        ).status_code
        == 201
    )
    assert (
        _upload(
            client, ext_headers, ext_listing["id"], make_image_bytes("PNG", "B")
        ).status_code
        == 201
    )
    assert Listing.objects.get(pk=ext_listing["id"]).import_status == "complete"


def test_photo_download_roundtrip(operator_client, csrf_client, ext_headers, png_bytes):
    created = post_json(
        operator_client,
        "/api/listings",
        {
            "source_url": "https://example.test/l/dl",
            "source_site": "example.test",
            "title": "download probe",
        },
    )
    assert created.status_code == 201, created.content
    listing_id = created.json()["id"]
    uploaded = operator_client.post(
        f"/api/listings/{listing_id}/photos",
        data={"file": SimpleUploadedFile("a.png", png_bytes, "image/png")},
        headers={"X-CSRFToken": operator_client.csrf_token},
    )
    assert uploaded.status_code == 201, uploaded.content
    photo_id = uploaded.json()["id"]

    fetched = operator_client.get(f"/api/listings/{listing_id}/photos/{photo_id}")
    assert fetched.status_code == 200
    assert fetched["Content-Type"] == "image/png"
    assert b"".join(fetched.streaming_content) == png_bytes


def test_unauthorized_evidence_access_denied(
    client, ext_headers, ext_listing, png_bytes
):
    assert _upload(client, ext_headers, ext_listing["id"], png_bytes).status_code == 201
    photo = Photograph.objects.get()
    # No session at all.
    assert (
        client.get(f"/api/listings/{ext_listing['id']}/photos/{photo.id}").status_code
        == 401
    )
    # Extension token is not valid on mother-app endpoints.
    assert (
        client.get(
            f"/api/listings/{ext_listing['id']}/photos/{photo.id}", headers=ext_headers
        ).status_code
        == 401
    )


def test_photo_list_endpoint(operator_client, client, ext_headers, png_bytes):
    created = post_json(
        operator_client,
        "/api/listings",
        {
            "source_url": "https://example.test/l/gallery",
            "source_site": "example.test",
            "title": "gallery probe",
        },
    )
    listing_id = created.json()["id"]
    assert operator_client.get(f"/api/listings/{listing_id}/photos").json() == []
    uploaded = operator_client.post(
        f"/api/listings/{listing_id}/photos",
        data={"file": SimpleUploadedFile("a.png", png_bytes, "image/png")},
        headers={"X-CSRFToken": operator_client.csrf_token},
    )
    assert uploaded.status_code == 201
    listed = operator_client.get(f"/api/listings/{listing_id}/photos").json()
    assert len(listed) == 1
    assert listed[0]["id"] == uploaded.json()["id"]
    assert listed[0]["sha256"] == uploaded.json()["sha256"]
    # Anonymous + extension-token callers are denied.
    from django.test import Client as DjangoClient

    anon = DjangoClient(enforce_csrf_checks=True)  # operator_client shares `client`
    assert anon.get(f"/api/listings/{listing_id}/photos").status_code == 401
    assert (
        anon.get(f"/api/listings/{listing_id}/photos", headers=ext_headers).status_code
        == 401
    )


def test_unknown_photo_is_404(operator_client, ext_listing):
    assert (
        operator_client.get(
            f"/api/listings/{ext_listing['id']}/photos/00000000-0000-0000-0000-000000000000"
        ).status_code
        == 404
    )


def test_manual_upload_requires_csrf(operator_client, png_bytes):
    created = post_json(
        operator_client,
        "/api/listings",
        {
            "source_url": "https://example.test/l/csrf",
            "source_site": "example.test",
            "title": "t",
        },
    )
    listing_id = created.json()["id"]
    no_token = operator_client.post(
        f"/api/listings/{listing_id}/photos",
        data={"file": SimpleUploadedFile("a.png", png_bytes, "image/png")},
    )
    assert no_token.status_code == 403
    assert Photograph.objects.count() == 0


def test_upload_to_unknown_listing_is_404(client, ext_headers, png_bytes):
    response = _upload(
        client, ext_headers, "00000000-0000-0000-0000-000000000000", png_bytes
    )
    assert response.status_code == 404
