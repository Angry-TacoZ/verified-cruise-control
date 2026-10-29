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
from django.db.models import F, Sum
from PIL import Image, ImageFile

from config import limits
from listings.models import Listing

from .models import PhotoFailure, Photograph, Provenance

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
    violations (not retried) and OperationalError on lock contention.

    Idempotency comes before capacity: an existing (listing, sha256) photo
    is returned even when the listing is already at its count or byte
    limits, so identical retries after a partial failure are not rejected.
    """
    with transaction.atomic():
        existing = Photograph.objects.filter(
            listing=listing, sha256=validated.sha256
        ).first()
        if existing is not None:
            return existing, True

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
        resolve_failures_for_photo(
            listing, sha256=validated.sha256, source_url=source_url
        )
        listing.refresh_import_status()
        return photo, duplicate
    assert last_error is not None
    try:
        original_path(storage_ref, validated.image_format).unlink(missing_ok=True)
    except OSError:
        pass
    raise last_error


def failure_key_for_sha(sha256: str) -> str:
    """Failure key for bytes that reached the server (validation/upload)."""
    return sha256


def failure_key_for_url(source_url: str) -> str:
    """Failure key for photos that never reached the server (download/
    permission): derived from the URL since no bytes exist to hash."""
    return "url:" + hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def record_failure(
    listing: Listing,
    *,
    key: str,
    kind: str,
    detail: str = "",
    source_url: str | None = None,
) -> tuple[PhotoFailure, bool]:
    """Idempotent per-photograph failure record. Returns (failure, created).

    Only the first unresolved report for a (listing, key) increments
    ``failed_upload_count`` — via an atomic ``F()`` update, never an
    in-memory value — so concurrent and repeated reports cannot inflate the
    count. Re-reporting a resolved failure reopens it (a genuine new
    failure), which also increments exactly once.

    Retries on lock contention like the photo insert path: concurrent
    failure reports for one listing contend on SQLite, so each attempt
    works from a fresh snapshot.
    """
    last_error: OperationalError | None = None
    for attempt in range(WRITE_ATTEMPTS):
        try:
            with transaction.atomic():
                failure, created = (
                    PhotoFailure.objects.select_for_update().get_or_create(
                        listing=listing,
                        key=key,
                        defaults={
                            "kind": kind,
                            "detail": detail,
                            "source_url": source_url,
                        },
                    )
                )
                if created:
                    Listing.objects.filter(pk=listing.pk).update(
                        failed_upload_count=F("failed_upload_count") + 1
                    )
                    listing.refresh_from_db(fields=["failed_upload_count"])
                else:
                    updates: dict = {"detail": detail}
                    if source_url is not None and failure.source_url != source_url:
                        updates["source_url"] = source_url
                    if failure.kind != kind:
                        updates["kind"] = kind
                    if failure.resolved:
                        # A new failure after recovery reopens the record.
                        updates["resolved"] = False
                        Listing.objects.filter(pk=listing.pk).update(
                            failed_upload_count=F("failed_upload_count") + 1
                        )
                        listing.refresh_from_db(fields=["failed_upload_count"])
                    PhotoFailure.objects.filter(pk=failure.pk).update(**updates)
                    failure.refresh_from_db()
            listing.refresh_import_status()
            return failure, created
        except OperationalError as exc:
            last_error = exc
            time.sleep(0.05 * (2**attempt) + random.uniform(0, 0.05))
            continue
    assert last_error is not None
    raise last_error


def resolve_failures_for_photo(
    listing: Listing,
    *,
    sha256: str,
    source_url: str | None,
) -> int:
    """Mark failures resolved once the photo stores successfully. Matches
    both the byte-hash key and the pre-upload URL key, so a photo that
    previously failed to download resolves when it finally uploads."""
    candidates = [failure_key_for_sha(sha256)]
    if source_url:
        candidates.append(failure_key_for_url(source_url))
    last_error: OperationalError | None = None
    resolved_count = 0
    for attempt in range(WRITE_ATTEMPTS):
        try:
            with transaction.atomic():
                matching = PhotoFailure.objects.select_for_update().filter(
                    listing=listing, resolved=False, key__in=candidates
                )
                resolved_count = matching.update(resolved=True)
                if resolved_count:
                    Listing.objects.filter(pk=listing.pk).update(
                        failed_upload_count=F("failed_upload_count") - resolved_count
                    )
                    listing.refresh_from_db(fields=["failed_upload_count"])
                    # Clamp: concurrent writers each decrement for rows they
                    # flipped, but a stale reopen must never drive the
                    # counter negative.
                    if listing.failed_upload_count < 0:
                        Listing.objects.filter(pk=listing.pk).update(
                            failed_upload_count=0
                        )
                        listing.refresh_from_db(fields=["failed_upload_count"])
            break
        except OperationalError as exc:
            last_error = exc
            time.sleep(0.05 * (2**attempt) + random.uniform(0, 0.05))
            continue
    else:
        assert last_error is not None
        raise last_error
    listing.refresh_import_status()
    return resolved_count


def read_bounded_upload(file, limit: int) -> bytes:
    """Read an uploaded file with a hard byte cap. Raises ImageRejected for
    oversize/empty payloads so callers keep one consistent error path.

    The declared size is checked first to reject obvious oversize uploads
    without reading; the chunked loop then enforces the cap even when size
    metadata is missing or lies. Underlying I/O errors propagate to the
    caller (mapped to HTTP 400 there), never masked as rejections.
    """
    declared = getattr(file, "size", None)
    if declared is not None and declared > limit:
        raise ImageRejected("too_large", "photograph exceeds size limit")
    chunks: list[bytes] = []
    total = 0
    for chunk in file.chunks(chunk_size=256 * 1024):
        total += len(chunk)
        if total > limit:
            raise ImageRejected("too_large", "photograph exceeds size limit")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise ImageRejected("empty", "photograph is empty")
    return data


PROVENANCE_VALUES = {Provenance.EXTENSION, Provenance.MANUAL}
