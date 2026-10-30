"""Listing validation + creation. Untrusted dealership input enters here."""

from __future__ import annotations

from urllib.parse import urlparse

from ninja.errors import HttpError

from config import limits

from .models import Listing

VIN_ALLOWED = frozenset("ABCDEFGHJKLMNPRSTUVWXYZ0123456789")


def normalize_url(value: str, *, field: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > limits.MAX_URL_CHARS:
        raise HttpError(422, f"{field} must be 1..{limits.MAX_URL_CHARS} chars")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HttpError(422, f"{field} must be an http(s) URL")
    return value


def normalize_claims(claims: list[str] | None) -> list[str]:
    claims = claims or []
    if len(claims) > limits.MAX_CLAIMS:
        raise HttpError(422, f"at most {limits.MAX_CLAIMS} advertised claims")
    cleaned = []
    for claim in claims:
        text = str(claim).strip()
        if not text:
            continue
        if len(text) > limits.MAX_CLAIM_CHARS:
            raise HttpError(422, "advertised claim too long")
        cleaned.append(text)
    return cleaned


def normalize_vin(vin: str | None) -> str | None:
    if vin is None:
        return None
    vin = vin.strip().upper()
    if vin == "":
        return None
    if len(vin) != 17 or any(ch not in VIN_ALLOWED for ch in vin):
        raise HttpError(422, "vin must be 17 chars (no I, O, Q)")
    return vin


def normalize_year(year: int | None) -> int | None:
    if year is None:
        return None
    if year < 1886 or year > 2100:
        raise HttpError(422, "year out of range")
    return year


def create_listing(payload) -> Listing:
    """Persist a listing from a validated ``ListingIn`` schema object."""
    description = payload.description or ""
    if len(description) > limits.MAX_LISTING_TEXT_CHARS:
        raise HttpError(422, "description too long")
    expected = payload.expected_photo_count
    if expected is not None and not 0 <= expected <= limits.MAX_PHOTOS_PER_LISTING:
        raise HttpError(422, "expected_photo_count out of range")
    return Listing.objects.create(
        source_url=normalize_url(payload.source_url, field="source_url"),
        source_site=payload.source_site.strip(),
        year=normalize_year(payload.year),
        make=(payload.make or "").strip(),
        model=(payload.model or "").strip(),
        trim=(payload.trim or "").strip(),
        vin=normalize_vin(payload.vin),
        title=payload.title.strip(),
        description=description,
        advertised_claims=normalize_claims(payload.advertised_claims),
        expected_photo_count=expected,
    )
