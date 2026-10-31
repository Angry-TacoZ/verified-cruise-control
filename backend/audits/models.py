"""Audit models: durable jobs, per-call ledger, observations, findings.

Money uses Decimal throughout; a null ``cost_usd`` on an execution means the
cost is UNKNOWN, never zero. Budget enforcement halts the job rather than
treating unknown spend as free.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models


class JobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    PAUSED = "paused", "Paused (budget or blocker)"
    CANCELLED = "cancelled", "Cancelled"
    COMPLETE = "complete", "Complete"
    FAILED = "failed", "Failed"


class Stage(models.TextChoices):
    SCREENING = "screening", "Screening"
    Muse = "muse", "Muse observation"
    SAM = "sam", "SAM localization"
    CROP = "crop", "Crop derivation"
    RECHECK = "recheck", "Crop recheck"
    RECONCILE = "reconcile", "Deterministic reconcile"


class Provider(models.TextChoices):
    BASETEN = "baseten", "Baseten (Muse Spark)"
    META = "meta", "Meta (SAM)"
    LOCAL = "local", "Local (no provider call)"


class ObservationSource(models.TextChoices):
    Muse = "muse", "Muse"
    SAM = "sam", "SAM"
    RECHECK = "recheck", "Crop recheck"
    AUTHORITATIVE = "authoritative", "Authoritative vehicle-specific evidence"


class Verdict(models.TextChoices):
    PRESENT = "present", "Feature appears present"
    ABSENT = "absent", "Feature appears absent"
    UNCLEAR = "unclear", "Cannot tell from this photo"


class EvidenceState(models.TextChoices):
    """Five-state taxonomy (README / AGENTS.md Rule E)."""

    VERIFIED = "verified", "Verified"
    VISUALLY_SUPPORTED = "visually_supported", "Visually supported"
    CONTRADICTED = "contradicted", "Contradicted"
    INSUFFICIENT = "insufficient_evidence", "Insufficient evidence"
    NOT_VISUALLY_VERIFIABLE = "not_visually_verifiable", "Not visually verifiable"


class AuditJob(models.Model):
    """One audit of a listing. Queueing reuses an in-flight job for the same
    listing instead of creating a duplicate (see ``audits.services``)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    listing = models.ForeignKey(
        "listings.Listing", on_delete=models.CASCADE, related_name="audit_jobs"
    )
    status = models.CharField(
        max_length=16, choices=JobStatus.choices, default=JobStatus.QUEUED
    )
    requested_by = models.CharField(max_length=255, blank=True, default="")
    prompt_version = models.CharField(max_length=32, default="m1")
    pricing_version = models.CharField(max_length=32, default="2026-10-31/09-20")
    max_calls = models.PositiveIntegerField(default=60)
    spend_cap_usd = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal("2.0000")
    )
    calls_made = models.PositiveIntegerField(default=0)
    spend_usd = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal("0.0000")
    )
    unknown_cost_calls = models.PositiveIntegerField(default=0)
    worker_id = models.CharField(max_length=64, blank=True, default="")
    leased_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One reusable (queued/running) audit per listing, enforced by
            # the database so concurrent submissions cannot duplicate paid
            # work. Paused/terminal jobs leave the index, allowing history
            # and fresh retries.
            models.UniqueConstraint(
                fields=["listing"],
                condition=models.Q(status__in=("queued", "running")),
                name="unique_active_audit_per_listing",
            )
        ]

    @property
    def current_stage(self) -> str:
        latest = self.executions.order_by("-created_at").first()
        if latest is not None:
            return latest.stage
        if self.status == JobStatus.RUNNING:
            return Stage.SCREENING
        return self.status


class AuditExecution(models.Model):
    """One provider call (or local stage) in the usage ledger."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        AuditJob, on_delete=models.CASCADE, related_name="executions"
    )
    stage = models.CharField(max_length=16, choices=Stage.choices)
    provider = models.CharField(
        max_length=16, choices=Provider.choices, default=Provider.LOCAL
    )
    model_id = models.CharField(max_length=255, blank=True, default="")
    photo_ids = models.JSONField(default=list)
    ok = models.BooleanField(default=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    usage = models.JSONField(default=dict)
    cost_usd = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True
    )
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class Observation(models.Model):
    """One normalized per-photo, per-claim model observation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        AuditJob, on_delete=models.CASCADE, related_name="observations"
    )
    claim_key = models.CharField(max_length=255)
    photo = models.ForeignKey(
        "evidence.Photograph",
        on_delete=models.CASCADE,
        related_name="audit_observations",
    )
    source = models.CharField(max_length=16, choices=ObservationSource.choices)
    verdict = models.CharField(max_length=16, choices=Verdict.choices)
    boxes = models.JSONField(default=list)
    rationale = models.TextField(blank=True, default="")
    extra = models.JSONField(default=dict)
    execution = models.ForeignKey(
        AuditExecution,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="observations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "claim_key", "photo", "source"],
                name="unique_observation_per_job_claim_photo_source",
            )
        ]


class AuditCrop(models.Model):
    """One derived crop image cut from a SAM mask bounding region."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(AuditJob, on_delete=models.CASCADE, related_name="crops")
    source_photo = models.ForeignKey(
        "evidence.Photograph",
        on_delete=models.CASCADE,
        related_name="audit_crops",
    )
    box = models.JSONField(default=dict)
    storage_ref = models.CharField(max_length=64, unique=True)
    mask_storage_ref = models.CharField(max_length=64, null=True, blank=True)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class AuditFinding(models.Model):
    """One reconciled per-claim finding for a job."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(AuditJob, on_delete=models.CASCADE, related_name="findings")
    claim_key = models.CharField(max_length=255)
    state = models.CharField(max_length=32, choices=EvidenceState.choices)
    summary = models.TextField(blank=True, default="")
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["claim_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "claim_key"],
                name="unique_finding_per_job_claim",
            )
        ]
