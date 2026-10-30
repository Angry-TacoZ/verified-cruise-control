"""Bounded upload reads: oversize payloads rejected without full buffering."""

from __future__ import annotations

import io
import os

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from config import limits
from evidence import images
from evidence.models import Photograph
from listings.models import Listing

pytestmark = pytest.mark.django_db


class FakeUpload:
    """File-like with controllable declared size and a read counter."""

    def __init__(self, chunks, declared_size):
        self._chunks = list(chunks)
        self.size = declared_size
        self.chunks_read = 0

    def chunks(self, chunk_size=256 * 1024):
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk


def _noisy_jpeg(target_min: int) -> bytes:
    """Random-pixel JPEG comfortably above target_min bytes."""
    rng = os.urandom(640 * 480 * 3)
    img = Image.frombytes("RGB", (640, 480), rng)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    data = buf.getvalue()
    assert len(data) > target_min, f"fixture too small: {len(data)}"
    return data


def test_valid_upload_just_below_limit(client, ext_headers, ext_listing, monkeypatch):
    data = _noisy_jpeg(60_000)
    monkeypatch.setattr(limits, "MAX_PHOTO_BYTES", len(data))
    response = client.post(
        f"/api/extension/listings/{ext_listing['id']}/photos",
        data={"file": SimpleUploadedFile("big.jpg", data, "image/jpeg")},
        headers=ext_headers,
    )
    assert response.status_code == 201, response.content
    assert response.json()["file_size"] == len(data)


def test_oversized_upload_rejected_before_reading(monkeypatch):
    """Declared size over the limit rejects without consuming any chunks."""
    fake = FakeUpload([b"x" * 1024] * 100, declared_size=limits.MAX_PHOTO_BYTES + 1)
    with pytest.raises(images.ImageRejected) as exc:
        images.read_bounded_upload(fake, limits.MAX_PHOTO_BYTES)
    assert exc.value.code == "too_large"
    assert fake.chunks_read == 0


def test_upload_crossing_limit_mid_stream_rejected(monkeypatch):
    """A lying (too-small) declared size cannot smuggle an oversize body:
    the chunked loop caps memory and rejects mid-stream."""
    monkeypatch.setattr(limits, "MAX_PHOTO_BYTES", 1024)
    fake = FakeUpload([b"y" * 512] * 6, declared_size=100)
    with pytest.raises(images.ImageRejected) as exc:
        images.read_bounded_upload(fake, limits.MAX_PHOTO_BYTES)
    assert exc.value.code == "too_large"
    # Bounded: never buffered more than limit + one chunk.
    assert fake.chunks_read * 512 <= 1024 + 512


def test_absent_size_metadata_still_bounded(monkeypatch):
    monkeypatch.setattr(limits, "MAX_PHOTO_BYTES", 100)
    # No declared size: falls back to chunked reading, small body passes.
    fake = FakeUpload([b"z" * 60], declared_size=None)
    assert images.read_bounded_upload(fake, limits.MAX_PHOTO_BYTES) == b"z" * 60


def test_empty_upload_rejected(client, ext_headers, ext_listing):
    response = client.post(
        f"/api/extension/listings/{ext_listing['id']}/photos",
        data={"file": SimpleUploadedFile("empty.png", b"", "image/png")},
        headers=ext_headers,
    )
    assert response.status_code == 422
    assert Photograph.objects.count() == 0


def test_rejected_upload_leaves_no_evidence(
    client, ext_headers, ext_listing, png_bytes
):
    """Oversize and corrupt uploads leave neither photo rows nor failure
    rows that could masquerade as evidence (read-level rejections carry no
    stable photo identity, so nothing is recorded)."""
    big = png_bytes + b"\0" * (limits.MAX_PHOTO_BYTES + 1)
    response = client.post(
        f"/api/extension/listings/{ext_listing['id']}/photos",
        data={"file": SimpleUploadedFile("big.png", big, "image/png")},
        headers=ext_headers,
    )
    assert response.status_code == 413
    assert Photograph.objects.count() == 0
    listing = Listing.objects.get(pk=ext_listing["id"])
    assert listing.photo_failures.count() == 0
    assert listing.failed_upload_count == 0
