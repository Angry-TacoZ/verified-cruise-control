"""Deterministic wrong-vehicle photo filter.

Dealer gallery imports often bleed in "recommended vehicle" photos whose
filenames name a different car (e.g. ``2020_dodge_challenger-pic-...`` next
to ``2024_mini_cooper-pic-...``). This matcher compares the (year, make,
model) triple embedded in a filename against the listing vehicle and flags
photos that provably show a different car — before any provider call.

Fail-open by design, because dealer filenames are unreliable:

- No recognizable triple in the filename -> KEEP (ambiguous, not evidence).
- Listing vehicle incomplete (no year/make/model) -> KEEP everything.
- Triple matches (prefix-tolerant on make/model for trim variants like
  ``cooper`` vs ``coopers``) -> KEEP.
- Triple present but year/make/model disagree -> EXCLUDE as wrong-vehicle.

Exclusions are always ledger-recorded with the filename; nothing is
silently dropped, and originals on disk are never touched.
"""

from __future__ import annotations

import re

_TRIPLE_RE = re.compile(r"(\d{4})[_-]([a-z0-9]+)[_-]([a-z0-9]+)")


def slug(text: str | None) -> str:
    """Lowercase alphanumeric slug; empty string for missing values."""
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def filename_vehicle(filename: str | None) -> tuple[str, str, str] | None:
    """Extract a (year, make, model) triple from a filename, if present."""
    if not filename:
        return None
    match = _TRIPLE_RE.search(filename.lower())
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _token_matches(file_token: str, listing_token: str) -> bool:
    """Equal or prefix-tolerant (trim variants), never empty-on-empty."""
    if not file_token or not listing_token:
        return False
    return file_token == listing_token or file_token.startswith(listing_token)


def is_wrong_vehicle(
    filename: str | None, year: object, make: str | None, model: str | None
) -> bool:
    """True only when the filename provably names a different vehicle."""
    triple = filename_vehicle(filename)
    if triple is None:
        return False
    if year is None:
        return False
    file_year, file_make, file_model = triple
    listing_year = str(year)
    listing_make = slug(make)
    listing_model = slug(model)
    if not listing_make or not listing_model:
        return False
    if file_year != listing_year:
        return True
    if not _token_matches(file_make, listing_make):
        return True
    model_ok = _token_matches(file_model, listing_model) or _token_matches(
        listing_model, file_model
    )
    return not model_ok
