"""Per-photograph failure records: idempotent, atomic, resolvable."""

from __future__ import annotations

import json
import threading

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import Client as DjangoClient

from evidence import images
from evidence.models import Photograph
from listings.models import Listing

from .conftest import make_image_bytes, post_json

pytestmark = pytest.mark.django_db


def _report(client, headers, listing_id, payload):
    return client.post(
        f"/api/extension/listings/{listing_id}/failed-photos",
        data=json.dumps(payload),
        content_type="application/json",
        headers=headers,
    )


def _photo_file(name: str, data: bytes, ctype: str = "image/png"):
    return SimpleUploadedFile(name, data, ctype)


DOWNLOAD = {
    "source_url": "http://127.0.0.1:8901/photos/missing.jpg",
    "kind": "download",
    "detail": "image download failed (404)",
}


def test_download_failure_reported_and_listed(
    client, ext_headers, ext_listing, operator_client
):
    response = _report(client, ext_headers, ext_listing["id"], DOWNLOAD)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["kind"] == "download"
    assert body["resolved"] is False

    listing = Listing.objects.get(pk=ext_listing["id"])
    assert listing.failed_upload_count == 1
    assert listing.import_status == "partial"

    failures = operator_client.get(
        f"/api/listings/{ext_listing['id']}/failed-photos"
    ).json()
    assert len(failures) == 1
    assert failures[0]["kind"] == "download"
    assert failures[0]["source_url"] == DOWNLOAD["source_url"]
    assert failures[0]["resolved"] is False


def test_repeat_report_does_not_double_count(client, ext_headers, ext_listing):
    assert _report(client, ext_headers, ext_listing["id"], DOWNLOAD).status_code == 201
    assert _report(client, ext_headers, ext_listing["id"], DOWNLOAD).status_code == 201
    assert (
        _report(
            client, ext_headers, ext_listing["id"], {**DOWNLOAD, "detail": "still 404"}
        ).status_code
        == 201
    )
    listing = Listing.objects.get(pk=ext_listing["id"])
    assert listing.failed_upload_count == 1
    assert listing.photo_failures.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_distinct_failures_count_each(client, ext_headers, ext_listing):
    errors: list[Exception] = []

    def _one(i: int) -> None:
        try:
            thread_client = DjangoClient(enforce_csrf_checks=True)
            response = _report(
                thread_client,
                ext_headers,
                ext_listing["id"],
                {
                    "source_url": f"http://127.0.0.1:8901/photos/gone-{i}.jpg",
                    "kind": "download",
                    "detail": "404",
                },
            )
            assert response.status_code == 201
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=_one, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert Listing.objects.get(pk=ext_listing["id"]).failed_upload_count == 2
    Listing.objects.get(pk=ext_listing["id"]).delete()


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_failure_counts_once(client, ext_headers, ext_listing):
    errors: list[Exception] = []

    def _one() -> None:
        try:
            thread_client = DjangoClient(enforce_csrf_checks=True)
            response = _report(thread_client, ext_headers, ext_listing["id"], DOWNLOAD)
            assert response.status_code == 201
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=_one) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    listing = Listing.objects.get(pk=ext_listing["id"])
    assert listing.photo_failures.count() == 1
    assert listing.failed_upload_count == 1
    listing.delete()


def test_successful_upload_resolves_prior_failure(
    client, ext_headers, ext_listing, listing_payload, png_bytes
):
    """A listing expecting exactly one photo proves full status recovery:
    report a download failure, upload the photo, expect resolved + complete."""
    listing_payload["expected_photo_count"] = 1
    created = client.post(
        "/api/extension/listings",
        data=json.dumps(listing_payload),
        content_type="application/json",
        headers=ext_headers,
    )
    assert created.status_code == 201, created.content
    listing_id = created.json()["id"]
    assert _report(client, ext_headers, listing_id, DOWNLOAD).status_code == 201
    response = client.post(
        f"/api/extension/listings/{listing_id}/photos",
        data={
            "file": _photo_file("missing.jpg", png_bytes, "image/png"),
            "source_url": DOWNLOAD["source_url"],
        },
        headers=ext_headers,
    )
    assert response.status_code == 201, response.content
    listing = Listing.objects.get(pk=listing_id)
    failure = listing.photo_failures.get()
    assert failure.resolved is True
    assert listing.failed_upload_count == 0
    assert listing.import_status == "complete"
    assert Photograph.objects.count() == 1


def test_validation_failure_kind_stored(client, ext_headers, ext_listing):
    response = client.post(
        f"/api/extension/listings/{ext_listing['id']}/photos",
        data={"file": _photo_file("x.png", b"not an image", "image/png")},
        headers=ext_headers,
    )
    assert response.status_code == 422
    failure = Listing.objects.get(pk=ext_listing["id"]).photo_failures.get()
    assert failure.kind == "validation"
    assert failure.resolved is False


def test_failure_after_recovery_reopens(client, ext_headers, ext_listing, png_bytes):
    assert _report(client, ext_headers, ext_listing["id"], DOWNLOAD).status_code == 201
    uploaded = client.post(
        f"/api/extension/listings/{ext_listing['id']}/photos",
        data={
            "file": _photo_file("missing.jpg", png_bytes, "image/png"),
            "source_url": DOWNLOAD["source_url"],
        },
        headers=ext_headers,
    )
    assert uploaded.status_code == 201
    assert Listing.objects.get(pk=ext_listing["id"]).failed_upload_count == 0
    # A genuinely new failure for the same photo reopens the record once.
    assert _report(client, ext_headers, ext_listing["id"], DOWNLOAD).status_code == 201
    listing = Listing.objects.get(pk=ext_listing["id"])
    assert listing.photo_failures.count() == 1
    assert listing.photo_failures.get().resolved is False
    assert listing.failed_upload_count == 1


def test_unknown_kind_and_missing_identity_rejected(client, ext_headers, ext_listing):
    assert (
        _report(
            client, ext_headers, ext_listing["id"], {**DOWNLOAD, "kind": "alien"}
        ).status_code
        == 422
    )
    assert (
        _report(
            client, ext_headers, ext_listing["id"], {"kind": "download"}
        ).status_code
        == 422
    )
    assert Listing.objects.get(pk=ext_listing["id"]).failed_upload_count == 0


def test_stale_upload_refresh_does_not_clobber_partial(
    client, ext_headers, listing_payload, png_bytes
):
    """The concurrent-import race: an upload request fetched its listing
    object before a failure was recorded. Its success-path refresh must
    re-read the counter, not overwrite partial with photos_pending."""
    listing_payload["expected_photo_count"] = 3
    created = client.post(
        "/api/extension/listings",
        data=json.dumps(listing_payload),
        content_type="application/json",
        headers=ext_headers,
    )
    assert created.status_code == 201
    listing_id = created.json()["id"]
    # A request object as of listing creation: predates the first photo's
    # status advance AND the later failure (status importing, count 0).
    stale = Listing.objects.get(pk=listing_id)
    uploaded = client.post(
        f"/api/extension/listings/{listing_id}/photos",
        data={"file": _photo_file("a.png", png_bytes)},
        headers=ext_headers,
    )
    assert uploaded.status_code == 201
    assert _report(client, ext_headers, listing_id, DOWNLOAD).status_code == 201
    assert Listing.objects.get(pk=listing_id).import_status == "partial"
    second = client.post(
        f"/api/extension/listings/{listing_id}/photos",
        data={"file": _photo_file("b.png", make_image_bytes("PNG", "B"))},
        headers=ext_headers,
    )
    assert second.status_code == 201
    # The stale request object refreshes last: status must stay partial.
    stale.refresh_import_status()
    settled = Listing.objects.get(pk=listing_id)
    assert settled.failed_upload_count == 1
    assert settled.import_status == "partial"


def test_failure_endpoints_enforce_trust_boundaries(
    client, ext_headers, ext_listing, operator_client
):
    anon = DjangoClient(enforce_csrf_checks=True)
    # No session: denied on both endpoints.
    assert (
        anon.post(
            f"/api/extension/listings/{ext_listing['id']}/failed-photos",
            data="{}",
            content_type="application/json",
        ).status_code
        == 401
    )
    assert (
        anon.get(f"/api/listings/{ext_listing['id']}/failed-photos").status_code == 401
    )
    # Extension token is not valid on the mother-app endpoint.
    assert (
        anon.get(
            f"/api/listings/{ext_listing['id']}/failed-photos", headers=ext_headers
        ).status_code
        == 401
    )
    # Mother-app session cannot file extension failure reports (CSRF + auth).
    denied = post_json(
        operator_client,
        f"/api/extension/listings/{ext_listing['id']}/failed-photos",
        DOWNLOAD,
    )
    assert denied.status_code in (401, 403)


@pytest.mark.django_db(transaction=True)
def test_atomic_counter_under_direct_concurrency(ext_listing):
    """record_failure must not lose increments when rows are created
    concurrently for distinct photos (the old in-memory counter did)."""
    listing = Listing.objects.get(pk=ext_listing["id"])
    errors: list[Exception] = []

    def _one(i: int) -> None:
        try:
            images.record_failure(
                Listing.objects.get(pk=listing.pk),
                key=f"sha-{i}",
                kind="upload",
                detail="race",
            )
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=_one, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    listing.refresh_from_db()
    assert listing.failed_upload_count == 4
    listing.delete()
