"""Listing endpoints for both trust boundaries (shared service layer)."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.shortcuts import get_object_or_404
from ninja import Router, Schema, Status
from ninja.errors import HttpError
from ninja.pagination import paginate
from pydantic import Field

from config import limits
from pairing.auth import extension_auth, operator_auth

from . import services
from .models import ImportStatus, Listing

ext_router = Router(auth=extension_auth)
app_router = Router(auth=operator_auth)


class ListingIn(Schema):
    source_url: str = Field(max_length=limits.MAX_URL_CHARS)
    source_site: str = Field(max_length=255)
    year: int | None = None
    make: str = Field(default="", max_length=100)
    model: str = Field(default="", max_length=100)
    trim: str = Field(default="", max_length=100)
    vin: str | None = Field(default=None, max_length=17)
    title: str = Field(max_length=500)
    description: str = Field(default="", max_length=limits.MAX_LISTING_TEXT_CHARS)
    advertised_claims: list[str] = Field(default_factory=list)
    expected_photo_count: int | None = None


class ListingOut(Schema):
    id: uuid.UUID
    source_url: str
    source_site: str
    year: int | None
    make: str
    model: str
    trim: str
    vin: str | None
    title: str
    description: str
    advertised_claims: list
    expected_photo_count: int | None
    failed_upload_count: int
    photo_count: int = 0
    import_status: str
    created_at: datetime


def _serialize(listing: Listing) -> dict:
    return {
        "id": listing.id,
        "source_url": listing.source_url,
        "source_site": listing.source_site,
        "year": listing.year,
        "make": listing.make,
        "model": listing.model,
        "trim": listing.trim,
        "vin": listing.vin,
        "title": listing.title,
        "description": listing.description,
        "advertised_claims": listing.advertised_claims,
        "expected_photo_count": listing.expected_photo_count,
        "failed_upload_count": listing.failed_upload_count,
        "photo_count": listing.photos.count(),
        "import_status": listing.import_status,
        "created_at": listing.created_at,
    }


@ext_router.post("/listings", response={201: ListingOut})
def ext_create_listing(request, payload: ListingIn):
    if not payload.source_site.strip() or not payload.title.strip():
        raise HttpError(422, "source_site and title are required")
    return Status(201, _serialize(services.create_listing(payload)))


@app_router.post("/listings", response={201: ListingOut})
def app_create_listing(request, payload: ListingIn):
    """Manual mother-app import fallback (same validation as extension)."""
    if not payload.source_site.strip() or not payload.title.strip():
        raise HttpError(422, "source_site and title are required")
    return Status(201, _serialize(services.create_listing(payload)))


@app_router.get("/listings", response=list[ListingOut])
@paginate
def app_list_listings(request):
    return [_serialize(item) for item in Listing.objects.all()]


@app_router.get("/listings/{listing_id}", response=ListingOut)
def app_get_listing(request, listing_id: uuid.UUID):
    return _serialize(get_object_or_404(Listing, pk=listing_id))


assert ImportStatus  # re-export guard for router docs tooling
