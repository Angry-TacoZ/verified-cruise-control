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
