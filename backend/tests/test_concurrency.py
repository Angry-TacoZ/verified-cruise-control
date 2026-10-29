"""Write contention: concurrent photo uploads to one listing all persist."""

from __future__ import annotations

import threading

import pytest
from django.db import connections

from evidence import images
from evidence.models import Photograph
from listings.models import Listing

from .conftest import make_image_bytes

pytestmark = pytest.mark.django_db(transaction=True)


def test_concurrent_uploads_all_persist():
    listing = Listing.objects.create(
        source_url="https://example.test/l/race",
        source_site="example.test",
        title="concurrency race",
        expected_photo_count=6,
    )
    errors: list[Exception] = []

    def _upload(label: str) -> None:
        try:
            images.store_listing_photo(
                Listing.objects.get(pk=listing.pk),
                data=make_image_bytes("PNG", label),
                filename=f"{label}.png",
                source_url=None,
                provenance="extension",
            )
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=_upload, args=(f"R{i}",)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert Photograph.objects.filter(listing=listing).count() == 6
    # Status converges once writers quiesce.
    listing.refresh_from_db()
    listing.refresh_import_status()
    assert Listing.objects.get(pk=listing.pk).import_status == "complete"

    # transaction=True tests commit for real: clean up our rows.
    listing.delete()


def test_concurrent_duplicate_uploads_single_row_no_orphans():
    """Identical bytes raced from many threads resolve to one row: losers
    return the winner and delete their orphan files."""
    from django.conf import settings

    listing = Listing.objects.create(
        source_url="https://example.test/l/duplicate-race",
        source_site="example.test",
        title="duplicate race",
    )
    data = make_image_bytes("PNG", "SAME")
    errors: list[Exception] = []

    def _upload() -> None:
        try:
            images.store_listing_photo(
                Listing.objects.get(pk=listing.pk),
                data=data,
                filename="same.png",
                source_url=None,
                provenance="extension",
            )
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=_upload) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert Photograph.objects.filter(listing=listing).count() == 1
    photo = Photograph.objects.get(listing=listing)
    assert photo.file_size == len(data)
    originals = list(
        (settings.RUNTIME_DIR / "originals").glob(f"{photo.storage_ref}.*")
    )
    assert len(originals) == 1
    assert originals[0].read_bytes() == data

    listing.delete()
