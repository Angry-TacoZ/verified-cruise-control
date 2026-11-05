"""ACC audit scope: the audit snapshot holds only ACC-matching claims.

The dashboard may show all 66 seller claims, but the audit itself
reconciles the source claim of ACC against visual evidence of ACC.
Plain "cruise control" never enters the snapshot. When the seller
advertised no ACC wording, the snapshot holds the operator-requested
ACC label so the finding records that the source did not claim ACC.
"""

# ruff: noqa: F811 -- listing/photos/enabled are re-exported from
# test_audits so this module shares one fixture definition; each test
# function legitimately shadows them as fixture parameters.

from __future__ import annotations

import pytest

from audits import services
from listings.models import Listing

from .conftest import make_image_bytes
from .test_audits import enabled, listing, photos  # noqa: F401  (shared fixtures)

pytestmark = pytest.mark.django_db


def _listing_with(**overrides):
    params = {
        "source_url": "https://example.test/l/scope",
        "source_site": "example.test",
        "title": "scope listing",
        "advertised_claims": [],
        "expected_photo_count": 1,
    }
    params.update(overrides)
    return Listing.objects.create(**params)


def _photo(listing, filename):
    from evidence import images

    photo, _ = images.store_listing_photo(
        listing,
        data=make_image_bytes("PNG", filename),
        filename=filename,
        source_url=None,
        provenance="extension",
    )
    return photo


def test_acc_scope_keeps_only_acc_claims(listing, photos, enabled):
    job, created = services.queue_audit(listing.pk, scope="acc")
    assert created is True
    texts = [c["text"] for c in job.snapshot["claims"]]
    assert texts == ["adaptive cruise control"]


def test_plain_cruise_control_is_not_acc(enabled):
    listing = _listing_with(advertised_claims=["Cruise Control", "Bluetooth"])
    _photo(listing, "a.jpg")
    job, _ = services.queue_audit(listing.pk, scope="acc")
    texts = [c["text"] for c in job.snapshot["claims"]]
    assert texts == ["Adaptive cruise control (user-requested check)"]
    assert (
        "adaptive_cruise_control" in Listing.objects.get(pk=listing.pk).requested_checks
    )


def test_acc_scope_without_seller_text_uses_requested_label(enabled):
    listing = _listing_with(advertised_claims=["Bluetooth"])
    _photo(listing, "a.jpg")
    job, _ = services.queue_audit(listing.pk, scope="acc")
    texts = [c["text"] for c in job.snapshot["claims"]]
    assert texts == ["Adaptive cruise control (user-requested check)"]


def test_general_audit_still_snapshots_everything(listing, photos, enabled):
    job, _ = services.queue_audit(listing.pk)
    texts = [c["text"] for c in job.snapshot["claims"]]
    assert texts == ["adaptive cruise control", "lane keeping assist"]


def test_same_scope_reuses_job(listing, photos, enabled):
    first, created_first = services.queue_audit(listing.pk, scope="acc")
    second, created_second = services.queue_audit(listing.pk, scope="acc")
    assert created_first is True
    assert created_second is False
    assert first.pk == second.pk


def test_different_scope_conflicts_with_in_flight(listing, photos, enabled):
    services.queue_audit(listing.pk)
    with pytest.raises(services.ConflictingAuditError):
        services.queue_audit(listing.pk, scope="acc")


def test_full_scope_conflicts_with_in_flight_acc(listing, photos, enabled):
    services.queue_audit(listing.pk, scope="acc")
    with pytest.raises(services.ConflictingAuditError):
        services.queue_audit(listing.pk)


def test_unknown_scope_is_refused(listing, photos, enabled):
    with pytest.raises(services.UnknownClaimError):
        services.queue_audit(listing.pk, scope="everything")
