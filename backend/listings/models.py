"""Listing models: advertised vehicle data imported from dealership pages."""

from __future__ import annotations

import uuid

from django.db import models


class ImportStatus(models.TextChoices):
    IMPORTING = "importing", "Importing"
    PHOTOS_PENDING = "photos_pending", "Photos pending"
    PARTIAL = "partial", "Partial (some uploads failed)"
    COMPLETE = "complete", "Complete"
    FAILED = "failed", "Failed"


class Listing(models.Model):
    """One imported vehicle listing. VIN is optional; many listings omit it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_url = models.URLField(max_length=2048)
    source_site = models.CharField(max_length=255)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    make = models.CharField(max_length=100, blank=True, default="")
    model = models.CharField(max_length=100, blank=True, default="")
    trim = models.CharField(max_length=100, blank=True, default="")
    vin = models.CharField(max_length=17, null=True, blank=True)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True, default="")
    advertised_claims = models.JSONField(default=list)
    expected_photo_count = models.PositiveSmallIntegerField(null=True, blank=True)
    failed_upload_count = models.PositiveIntegerField(default=0)
    import_status = models.CharField(
        max_length=16,
        choices=ImportStatus.choices,
        default=ImportStatus.IMPORTING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def refresh_import_status(self) -> str:
        """Derive status from stored photos vs expected/failed counts.

        Rules: untouched -> importing; any failure -> partial; expected met
        with no failures -> complete; otherwise photos_pending when an
        expectation exists, else importing.

        Convergent under concurrency: concurrent uploads each recompute from
        committed state, so a burst may briefly show a stale status until the
        last writer's refresh lands. Photo rows and counts are always exact;
        callers needing a settled status should refresh after quiescence.
        """
        import random
        import time

        from django.db import OperationalError

        from evidence.models import Photograph

        last_error: OperationalError | None = None
        for attempt in range(3):
            try:
                return self._refresh_import_status_once(Photograph)
            except OperationalError as exc:
                last_error = exc
                time.sleep(0.05 * (2**attempt) + random.uniform(0, 0.05))
        assert last_error is not None
        raise last_error

    def _refresh_import_status_once(self, Photograph) -> str:
        succeeded = Photograph.objects.filter(listing=self).count()
        failed = self.failed_upload_count
        expected = self.expected_photo_count
        if succeeded == 0 and failed == 0:
            status = ImportStatus.IMPORTING
        elif failed > 0:
            status = ImportStatus.PARTIAL
        elif expected and succeeded >= expected:
            status = ImportStatus.COMPLETE
        elif expected:
            status = ImportStatus.PHOTOS_PENDING
        else:
            status = ImportStatus.IMPORTING
        if status != self.import_status:
            Listing.objects.filter(pk=self.pk).update(import_status=status)
            self.import_status = status
        return status
