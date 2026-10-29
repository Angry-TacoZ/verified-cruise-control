"""Photograph models: metadata lives here; bytes live on the filesystem."""

from __future__ import annotations

import uuid

from django.db import models


class Provenance(models.TextChoices):
    EXTENSION = "extension", "Browser extension upload"
    MANUAL = "manual", "Manual mother-app upload"


class UploadStatus(models.TextChoices):
    COMPLETE = "complete", "Complete"


class Photograph(models.Model):
    """Metadata for one stored original photograph.

    ``(listing, sha256)`` is unique so extension retries are idempotent: a
    re-uploaded original resolves to the existing row instead of duplicating
    evidence.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    listing = models.ForeignKey(
        "listings.Listing", on_delete=models.CASCADE, related_name="photos"
    )
    source_url = models.URLField(max_length=2048, null=True, blank=True)
    # Backend-generated storage identifier (never the uploader's filename).
    storage_ref = models.CharField(max_length=64, unique=True)
    sha256 = models.CharField(max_length=64, db_index=True)
    original_filename = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField()
    mime_type = models.CharField(max_length=64)
    image_format = models.CharField(max_length=8)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    provenance = models.CharField(
        max_length=16, choices=Provenance.choices, default=Provenance.EXTENSION
    )
    upload_status = models.CharField(
        max_length=16,
        choices=UploadStatus.choices,
        default=UploadStatus.COMPLETE,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["listing", "sha256"],
                name="unique_photo_per_listing",
            )
        ]
