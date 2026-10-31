"""Audit endpoints for the mother app (operator trust boundary only)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from ninja import Router, Schema, Status
from ninja.errors import HttpError

from listings.models import Listing
from pairing.auth import operator_auth

from . import pipeline, services, worker
from .models import (
    AuditCrop,
    AuditExecution,
    AuditFinding,
    AuditJob,
    JobStatus,
    Observation,
)

app_router = Router(auth=operator_auth)

TERMINAL = (JobStatus.COMPLETE, JobStatus.FAILED, JobStatus.CANCELLED)
RETRYABLE = TERMINAL + (JobStatus.PAUSED,)
MASK_REF_RE = re.compile(r"^[0-9a-f]{32}$")


class AuditJobOut(Schema):
    id: uuid.UUID
    listing_id: uuid.UUID
    status: str
    current_stage: str
    requested_by: str
    prompt_version: str
    pricing_version: str
    max_calls: int
    calls_made: int
    model_calls_made: int
    spend_cap_usd: str
    spend_usd: str
    unknown_cost_calls: int
    worker_id: str
    error_code: str
    error_detail: str
    observation_count: int = 0
    crop_count: int = 0
    finding_count: int = 0
    execution_count: int = 0
    created_at: datetime
    updated_at: datetime


class QueueOut(Schema):
    job: AuditJobOut
    created: bool


class FindingOut(Schema):
    id: uuid.UUID
    claim_key: str
    claim_text: str
    state: str
    summary: str
    evidence: dict
    created_at: datetime
    updated_at: datetime


class ObservationOut(Schema):
    id: uuid.UUID
    claim_key: str
    photo_id: uuid.UUID
    source: str
    verdict: str
    boxes: list
    rationale: str
    extra: dict
    created_at: datetime


class CropOut(Schema):
    id: uuid.UUID
    source_photo_id: uuid.UUID
    box: dict
    width: int
    height: int
    has_mask: bool = False


class ExecutionOut(Schema):
    id: uuid.UUID
    stage: str
    provider: str
    model_id: str
    photo_ids: list
    ok: bool
    attempts: int
    latency_ms: int | None
    usage: dict
    cost_usd: str | None
    error_code: str
    created_at: datetime


class EvidenceOut(Schema):
    observations: list[ObservationOut]
    crops: list[CropOut]
    executions: list[ExecutionOut]


def _serialize_job(job: AuditJob) -> dict:
    return {
        "id": job.id,
        "listing_id": job.listing_id,
        "status": job.status,
        "current_stage": job.current_stage,
        "requested_by": job.requested_by,
        "prompt_version": job.prompt_version,
        "pricing_version": job.pricing_version,
        "max_calls": job.max_calls,
        "calls_made": job.calls_made,
        "model_calls_made": job.model_calls_made,
        "spend_cap_usd": str(job.spend_cap_usd),
        "spend_usd": str(job.spend_usd),
        "unknown_cost_calls": job.unknown_cost_calls,
        "worker_id": job.worker_id,
        "error_code": job.error_code,
        "error_detail": job.error_detail,
        "observation_count": job.observations.count(),
        "crop_count": job.crops.count(),
        "finding_count": job.findings.count(),
        "execution_count": job.executions.count(),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


@app_router.post("/listings/{listing_id}/audits", response={202: QueueOut})
def app_queue_audit(request, listing_id: uuid.UUID):
    """Queue an audit (manual activation). Reuses an in-flight job for the
    same listing; spawns a worker pass only for a fresh job."""
    get_object_or_404(Listing, pk=listing_id)
    try:
        job, created = services.queue_audit(listing_id, requested_by="operator")
    except services.InferenceDisabledError as exc:
        raise HttpError(403, str(exc)) from exc
    if created:
        worker.spawn_worker()
    return Status(202, {"job": _serialize_job(job), "created": created})


@app_router.get("/listings/{listing_id}/audits", response=list[AuditJobOut])
def app_list_audits(request, listing_id: uuid.UUID):
    get_object_or_404(Listing, pk=listing_id)
    return [
        _serialize_job(job) for job in AuditJob.objects.filter(listing_id=listing_id)
    ]


@app_router.get("/audits/{job_id}", response=AuditJobOut)
def app_get_audit(request, job_id: uuid.UUID):
    return _serialize_job(get_object_or_404(AuditJob, pk=job_id))


@app_router.get("/audits/{job_id}/findings", response=list[FindingOut])
def app_get_findings(request, job_id: uuid.UUID):
    job = get_object_or_404(AuditJob, pk=job_id)
    return [
        {
            "id": finding.id,
            "claim_key": finding.claim_key,
            "claim_text": finding.claim_text,
            "state": finding.state,
            "summary": finding.summary,
            "evidence": finding.evidence,
            "created_at": finding.created_at,
            "updated_at": finding.updated_at,
        }
        for finding in AuditFinding.objects.filter(job=job)
    ]


@app_router.get("/audits/{job_id}/evidence", response=EvidenceOut)
def app_get_evidence(request, job_id: uuid.UUID):
    job = get_object_or_404(AuditJob, pk=job_id)
    return {
        "observations": [
            {
                "id": obs.id,
                "claim_key": obs.claim_key,
                "photo_id": obs.photo_id,
                "source": obs.source,
                "verdict": obs.verdict,
                "boxes": obs.boxes,
                "rationale": obs.rationale,
                "extra": obs.extra,
                "created_at": obs.created_at,
            }
            for obs in Observation.objects.filter(job=job)
        ],
        "crops": [
            {
                "id": crop.id,
                "source_photo_id": crop.source_photo_id,
                "box": crop.box,
                "width": crop.width,
                "height": crop.height,
                "has_mask": bool(crop.mask_storage_ref),
            }
            for crop in AuditCrop.objects.filter(job=job)
        ],
        "executions": [
            {
                "id": execution.id,
                "stage": execution.stage,
                "provider": execution.provider,
                "model_id": execution.model_id,
                "photo_ids": execution.photo_ids,
                "ok": execution.ok,
                "attempts": execution.attempts,
                "latency_ms": execution.latency_ms,
                "usage": execution.usage,
                "cost_usd": (
                    None if execution.cost_usd is None else str(execution.cost_usd)
                ),
                "error_code": execution.error_code,
                "created_at": execution.created_at,
            }
            for execution in AuditExecution.objects.filter(job=job)
        ],
    }


@app_router.get("/audits/{job_id}/crops/{crop_id}")
def app_download_crop(request, job_id: uuid.UUID, crop_id: uuid.UUID):
    """Derived crop bytes. IDs only — no caller-supplied paths."""
    crop = get_object_or_404(AuditCrop, pk=crop_id, job_id=job_id)
    path = pipeline.audit_dirs(job_id)["crops"] / f"{crop.storage_ref}.png"
    if not path.is_file():
        raise HttpError(410, "crop file missing from storage")
    return FileResponse(open(path, "rb"), content_type="image/png")


@app_router.get("/audits/{job_id}/masks/{ref}")
def app_download_mask(request, job_id: uuid.UUID, ref: str):
    """Derived mask bytes. Ref must be a hex storage id under this job."""
    get_object_or_404(AuditJob, pk=job_id)
    if not MASK_REF_RE.match(ref):
        raise HttpError(404, "unknown mask")
    path = pipeline.audit_dirs(job_id)["masks"] / f"{ref}.png"
    if not path.is_file():
        raise HttpError(410, "mask file missing from storage")
    return FileResponse(open(path, "rb"), content_type="image/png")


@app_router.post("/audits/{job_id}/cancel", response=AuditJobOut)
def app_cancel_audit(request, job_id: uuid.UUID):
    job = get_object_or_404(AuditJob, pk=job_id)
    if job.status in TERMINAL:
        raise HttpError(409, f"audit is already {job.status}")
    services.cancel_job(job_id)
    job.refresh_from_db()
    return _serialize_job(job)


@app_router.post("/audits/{job_id}/retry", response={202: QueueOut})
def app_retry_audit(request, job_id: uuid.UUID):
    """Queue a fresh audit for the same listing. Terminal and paused
    jobs can be retried; a paused job keeps its budget blocker on record
    while the fresh job starts clean."""
    job = get_object_or_404(AuditJob, pk=job_id)
    if job.status not in RETRYABLE:
        raise HttpError(409, "audit is still in flight")
    try:
        fresh, created = services.queue_audit(job.listing_id, requested_by="operator")
    except services.InferenceDisabledError as exc:
        raise HttpError(403, str(exc)) from exc
    if created:
        worker.spawn_worker()
    return Status(202, {"job": _serialize_job(fresh), "created": created})
