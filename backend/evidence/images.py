"""Image validation, original storage, and the photo-upload service.

Validation order is a security property (see AGENTS.md Rule H): compressed
size first, format allowlist, header dimensions with an explicit pixel cap
*before* any full decode, decompression-bomb warnings as errors, structural
verify, then reopen for further decode. Original bytes are preserved only
after validation passes.
"""

from __future__ import annotations

import hashlib
import io
import os
import random
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Sum
from PIL import Image, ImageFile

from config import limits
from listings.models import Listing

from .models import Photograph, Provenance

Image.MAX_IMAGE_PIXELS = limits.MAX_IMAGE_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = False

_FORMAT_EXTENSION = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
_FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class ImageRejected(Exception):
    """Raised when uploaded bytes are not an acceptable evidence image."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedImage:
    image_format: str
    width: int
    height: int
    mime_type: str
    file_size: int
    sha256: str


def storage_dirs() -> dict[str, Path]:
    runtime = Path(settings.RUNTIME_DIR)
    dirs = {
        "originals": runtime / "originals",
        "thumbnails": runtime / "derived" / "thumbnails",
        "crops": runtime / "derived" / "crops",
        "masks": runtime / "derived" / "masks",
        "temp": runtime / "temp",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def original_path(storage_ref: str, image_format: str) -> Path:
    ext = _FORMAT_EXTENSION[image_format]
    return storage_dirs()["originals"] / f"{storage_ref}.{ext}"


def validate_image_bytes(data: bytes) -> ValidatedImage:
    """Validate raw upload bytes. Never trusts extensions or MIME types."""
    if len(data) > limits.MAX_PHOTO_BYTES:
        raise ImageRejected("too_large", "photograph exceeds size limit")
    if not data:
        raise ImageRejected("empty", "photograph is empty")

    with warnings.catch_warnings():
        # A decompression bomb warning is a rejection, not a warning.
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            probe = Image.open(io.BytesIO(data))
        except Exception as exc:
            raise ImageRejected("unreadable", "not a readable image") from exc
        image_format = probe.format or ""
        if image_format not in limits.ALLOWED_IMAGE_FORMATS:
            raise ImageRejected("format", "unsupported image format")
        width, height = probe.size
        # Explicit pixel cap enforced from header dimensions, before decode.
        if width <= 0 or height <= 0 or width * height > limits.MAX_IMAGE_PIXELS:
            raise ImageRejected("dimensions", "image dimensions exceed limit")
        try:
            probe.verify()
        except Exception as exc:
            raise ImageRejected("corrupt", "image failed verification") from exc
        # verify() leaves the instance unusable; reopen for real decode.
        try:
            decoded = Image.open(io.BytesIO(data))
            decoded.load()
        except Exception as exc:
            raise ImageRejected("corrupt", "image is truncated or corrupt") from exc
        if decoded.format != image_format or decoded.size != (width, height):
            raise ImageRejected("inconsistent", "image headers inconsistent")

    return ValidatedImage(
        image_format=image_format,
        width=width,
        height=height,
        mime_type=_FORMAT_MIME[image_format],
        file_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def sanitize_filename(name: str) -> str:
    """Keep a display-safe basename for metadata (never used as a path)."""
    base = os.path.basename(name or "upload").strip().strip(".") or "upload"
    return base[:255]


def _write_original_atomically(data: bytes, dest: Path) -> None:
    tmp_path = storage_dirs()["temp"] / f".tmp-{uuid.uuid4().hex}"
    with open(tmp_path, "wb") as handle:
        handle.write(data)
    os.replace(tmp_path, dest)


WRITE_ATTEMPTS = 3


def _insert_photo_row(
    listing: Listing,
    *,
    data: bytes,
    validated: ValidatedImage,
    filename: str,
    source_url: str | None,
    provenance: str,
    storage_ref: str,
) -> tuple[Photograph, bool]:
    """Single transactional insert attempt. Raises ImageRejected for limit
    violations (not retried) and OperationalError on lock contention."""
    with transaction.atomic():
        if (
            Photograph.objects.filter(listing=listing).count()
            >= limits.MAX_PHOTOS_PER_LISTING
        ):
            raise ImageRejected("too_many", "listing already has maximum photos")
        total = (
            Photograph.objects.filter(listing=listing).aggregate(
                total=Sum("file_size")
            )["total"]
            or 0
        )
        if total + len(data) > limits.MAX_LISTING_PHOTO_BYTES:
            raise ImageRejected("listing_too_large", "listing photo payload too large")

        existing = Photograph.objects.filter(
            listing=listing, sha256=validated.sha256
        ).first()
        if existing is not None:
            return existing, True

        dest = original_path(storage_ref, validated.image_format)
        _write_original_atomically(data, dest)
        try:
            with transaction.atomic():
                photo = Photograph.objects.create(
                    listing=listing,
                    source_url=source_url,
                    storage_ref=storage_ref,
                    sha256=validated.sha256,
                    original_filename=sanitize_filename(filename),
                    file_size=validated.file_size,
                    mime_type=validated.mime_type,
                    image_format=validated.image_format,
                    width=validated.width,
                    height=validated.height,
                    provenance=provenance,
                )
        except IntegrityError:
            # Lost a duplicate race: drop our orphan file, return the winner.
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            photo = Photograph.objects.get(listing=listing, sha256=validated.sha256)
            return photo, True
        return photo, False


def store_listing_photo(
    listing: Listing,
    *,
    data: bytes,
    filename: str,
    source_url: str | None,
    provenance: str,
) -> tuple[Photograph, bool]:
    """Validate + store one photo. Returns (photo, duplicate).

    Validation runs before any database or disk write. The insert retries on
    lock contention (concurrent uploads to one listing) with a fresh snapshot
    per attempt; limit violations raise ImageRejected and are never retried.
    """
    validated = validate_image_bytes(data)
    storage_ref = uuid.uuid4().hex
    last_error: OperationalError | None = None
    for attempt in range(WRITE_ATTEMPTS):
        try:
            photo, duplicate = _insert_photo_row(
                listing,
                data=data,
                validated=validated,
                filename=filename,
                source_url=source_url,
                provenance=provenance,
                storage_ref=storage_ref,
            )
        except OperationalError as exc:
            last_error = exc
            time.sleep(0.05 * (2**attempt) + random.uniform(0, 0.05))
            continue
        listing.refresh_import_status()
        return photo, duplicate
    assert last_error is not None
    try:
        original_path(storage_ref, validated.image_format).unlink(missing_ok=True)
    except OSError:
        pass
    raise last_error


def record_failed_upload(listing: Listing) -> None:
    Listing.objects.filter(pk=listing.pk).update(
        failed_upload_count=listing.failed_upload_count + 1
    )
    listing.failed_upload_count += 1
    listing.refresh_import_status()


PROVENANCE_VALUES = {Provenance.EXTENSION, Provenance.MANUAL}
