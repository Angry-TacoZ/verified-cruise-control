"""Photo upload/download endpoints for both trust boundaries."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from ninja import File, Form, Router, Schema, Status
from ninja.errors import HttpError
from ninja.files import UploadedFile

from listings.models import Listing
from pairing.auth import extension_auth, operator_auth

from . import images
from .models import FailureKind, Photograph, Provenance

ext_router = Router(auth=extension_auth)
app_router = Router(auth=operator_auth)


class PhotoOut(Schema):
    id: uuid.UUID
    listing_id: uuid.UUID
    source_url: str | None
    sha256: str
    original_filename: str
    file_size: int
    mime_type: str
    image_format: str
    width: int
    height: int
    provenance: str
    upload_status: str
    duplicate: bool = False
    created_at: datetime


def _serialize(photo: Photograph, *, duplicate: bool = False) -> dict:
    return {
        "id": photo.id,
        "listing_id": photo.listing_id,
        "source_url": photo.source_url,
        "sha256": photo.sha256,
        "original_filename": photo.original_filename,
        "file_size": photo.file_size,
        "mime_type": photo.mime_type,
        "image_format": photo.image_format,
        "width": photo.width,
        "height": photo.height,
        "provenance": photo.provenance,
        "upload_status": photo.upload_status,
        "duplicate": duplicate,
        "created_at": photo.created_at,
    }


def _failure_kind_for_code(code: str) -> str:
    """Map an ImageRejected code to a stored failure kind. Capacity
    rejections mean the bytes were fine but the listing is full."""
    if code in ("too_large", "listing_too_large", "too_many"):
        return FailureKind.UPLOAD
    return FailureKind.VALIDATION


def _store_upload(
    request,
    listing_id: uuid.UUID,
    file: UploadedFile,
    source_url: str | None,
    provenance: str,
):
    from config import limits

    listing = get_object_or_404(Listing, pk=listing_id)
    try:
        data = images.read_bounded_upload(file, limits.MAX_PHOTO_BYTES)
    except images.ImageRejected as exc:
        raise HttpError(
            413 if exc.code == "too_large" else 422, f"{exc.code}: {exc}"
        ) from exc
    except Exception as exc:
        raise HttpError(400, "could not read upload") from exc
    clean_source_url = None
    if source_url:
        from listings.services import normalize_url

        clean_source_url = normalize_url(source_url, field="source_url")
    try:
        photo, duplicate = images.store_listing_photo(
            listing,
            data=data,
            filename=file.name or "upload",
            source_url=clean_source_url,
            provenance=provenance,
        )
    except images.ImageRejected as exc:
        images.record_failure(
            listing,
            key=images.failure_key_for_sha(hashlib.sha256(data).hexdigest()),
            kind=_failure_kind_for_code(exc.code),
            detail=f"{exc.code}: {exc}",
            source_url=clean_source_url,
        )
        status = 413 if exc.code in ("too_large", "listing_too_large") else 422
        raise HttpError(status, f"{exc.code}: {exc}") from exc
    return Status(201, _serialize(photo, duplicate=duplicate))


class FailedPhotoIn(Schema):
    """Extension report of a photo that never reached the upload endpoint
    (download or permission failure). ``key`` defaults to the URL-derived
    key when the reporter only knows the source URL."""

    source_url: str | None = None
    key: str | None = None
    kind: str
    detail: str = ""


class FailedPhotoOut(Schema):
    id: uuid.UUID
    listing_id: uuid.UUID
    key: str
    kind: str
    detail: str
    source_url: str | None
    resolved: bool
    created_at: datetime


def _serialize_failure(failure) -> dict:
    return {
        "id": failure.id,
        "listing_id": failure.listing_id,
        "key": failure.key,
        "kind": failure.kind,
        "detail": failure.detail,
        "source_url": failure.source_url,
        "resolved": failure.resolved,
        "created_at": failure.created_at,
    }


@ext_router.post("/listings/{listing_id}/failed-photos", response={201: FailedPhotoOut})
def ext_report_failure(request, listing_id: uuid.UUID, payload: FailedPhotoIn):
    """Idempotent failure report for photos that never reached the upload
    endpoint. Repeated reports of the same photo do not inflate the count."""
    from listings.services import normalize_url

    listing = get_object_or_404(Listing, pk=listing_id)
    if payload.kind not in FailureKind.values:
        raise HttpError(422, f"unknown failure kind: {payload.kind}")
    clean_source_url = None
    if payload.source_url:
        clean_source_url = normalize_url(payload.source_url, field="source_url")
    key = payload.key or (
        images.failure_key_for_url(clean_source_url) if clean_source_url else None
    )
    if not key:
        raise HttpError(422, "failure report needs a key or source_url")
    failure, _ = images.record_failure(
        listing,
        key=key[:132],
        kind=payload.kind,
        detail=payload.detail[:2000],
        source_url=clean_source_url,
    )
    return Status(201, _serialize_failure(failure))


@ext_router.post("/listings/{listing_id}/photos", response={201: PhotoOut})
def ext_upload_photo(
    request,
    listing_id: uuid.UUID,
    file: UploadedFile = File(...),  # noqa: B008 - required Ninja idiom
    source_url: str | None = Form(None),
):
    """One photograph per multipart request (extension)."""
    return _store_upload(request, listing_id, file, source_url, Provenance.EXTENSION)


@app_router.post("/listings/{listing_id}/photos", response={201: PhotoOut})
def app_upload_photo(
    request,
    listing_id: uuid.UUID,
    file: UploadedFile = File(...),  # noqa: B008 - required Ninja idiom
    source_url: str | None = Form(None),
):
    """One photograph per multipart request (manual mother-app fallback)."""
    return _store_upload(request, listing_id, file, source_url, Provenance.MANUAL)


@app_router.get("/listings/{listing_id}/photos", response=list[PhotoOut])
def app_list_photos(request, listing_id: uuid.UUID):
    """Photo metadata for the listing gallery (bytes via download route)."""
    listing = get_object_or_404(Listing, pk=listing_id)
    return [_serialize(photo) for photo in listing.photos.all()]


@app_router.get("/listings/{listing_id}/failed-photos", response=list[FailedPhotoOut])
def app_list_failures(request, listing_id: uuid.UUID):
    """Per-photograph failure outcomes for the mother app, resolved included
    so recovery stays visible."""
    listing = get_object_or_404(Listing, pk=listing_id)
    return [_serialize_failure(failure) for failure in listing.photo_failures.all()]


@app_router.get("/listings/{listing_id}/photos/{photo_id}")
def app_download_photo(request, listing_id: uuid.UUID, photo_id: uuid.UUID):
    """Authorized original bytes. IDs only — no caller-supplied paths."""
    photo = get_object_or_404(Photograph, pk=photo_id, listing_id=listing_id)
    path = images.original_path(photo.storage_ref, photo.image_format)
    if not path.is_file():
        raise HttpError(410, "original file missing from storage")
    return FileResponse(
        open(path, "rb"), content_type=photo.mime_type, as_attachment=False
    )
