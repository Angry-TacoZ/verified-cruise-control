"""Pairing models: extension credentials and local-operator bootstrap state.

Only verifiers (SHA-256 hex of high-entropy random tokens) are stored, never
plaintext credentials.
"""

from __future__ import annotations

import uuid

from django.db import models


class ExtensionPairing(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    label = models.CharField(max_length=100, blank=True, default="")
    token_verifier = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    rotated_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class LocalOperator(models.Model):
    """Singleton row (pk=1): has the local operator completed setup?"""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    initialized = models.BooleanField(default=False)
    # Verifier for the one-time setup token; cleared after initialization.
    setup_verifier = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
