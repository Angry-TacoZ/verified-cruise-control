"""Audit job lifecycle: queue (with reuse), atomic claim, heartbeat, cancel.

Budget rules fail closed: unknown cost pauses the job, and a stale worker
(differs from the recorded owner) can never write to it.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import AuditExecution, AuditJob, JobStatus

IN_FLIGHT = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED)
REUSABLE = (JobStatus.QUEUED, JobStatus.RUNNING)


class AuditError(Exception):
    """Base error for audit lifecycle failures."""


class InferenceDisabledError(AuditError):
    """Raised when queueing while inference is not explicitly enabled."""


class BudgetExceededError(AuditError):
    """Raised when a job hits its call cap, spend cap, or unknown cost."""

    def __init__(self, message: str, *, code: str = "budget", detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class StaleWorkerError(AuditError):
    """Raised when a worker that does not own the job tries to write."""


class JobNotRunningError(AuditError):
    """Raised when writing to a job that is no longer running."""


def queue_audit(listing_id, requested_by: str = "") -> tuple[AuditJob, bool]:
    """Queue an audit, reusing an in-flight job for the same listing.

    Returns ``(job, created)``. Refuses to queue when inference is not
    explicitly enabled.
    """
    if not settings.AUDIT_INFERENCE_ENABLED:
        raise InferenceDisabledError(
            "Inference is disabled; set VCC_INFERENCE_ENABLED=1 to queue audits."
        )
    existing = (
        AuditJob.objects.filter(listing_id=listing_id, status__in=REUSABLE)
        .order_by("-created_at")
        .first()
    )
    if existing is not None:
        return existing, False
    job = AuditJob.objects.create(
        listing_id=listing_id,
        requested_by=requested_by,
        max_calls=settings.AUDIT_MAX_CALLS_PER_JOB,
        spend_cap_usd=settings.AUDIT_SPEND_CAP_USD,
    )
    return job, True


def _claim_filter(worker_id: str, now):
    cutoff = now - timedelta(seconds=settings.AUDIT_WORKER_HEARTBEAT_S)
    return worker_id, now, cutoff


def claim_job(worker_id: str) -> AuditJob | None:
    """Atomically claim the oldest queued job, or recover a stale one.

    A RUNNING job whose heartbeat is older than the lease is treated as
    crashed and reclaimed. The conditional UPDATE is the atomic guard, so
    concurrent workers cannot claim the same job even on SQLite.
    Lock contention retries with backoff (same pattern as listings).
    """
    import random
    import time

    from django.db import OperationalError

    last_error: OperationalError | None = None
    for attempt in range(4):
        try:
            return _claim_once(worker_id)
        except OperationalError as exc:
            last_error = exc
            time.sleep(0.05 * (2**attempt) + random.uniform(0, 0.05))
    assert last_error is not None
    raise last_error


def _claim_once(worker_id: str) -> AuditJob | None:
    now = timezone.now()
    _, _, cutoff = _claim_filter(worker_id, now)
    with transaction.atomic():
        candidate = (
            AuditJob.objects.filter(status=JobStatus.QUEUED)
            .order_by("created_at")
            .first()
        )
        if candidate is not None:
            rows = AuditJob.objects.filter(
                pk=candidate.pk, status=JobStatus.QUEUED
            ).update(
                status=JobStatus.RUNNING,
                worker_id=worker_id,
                leased_at=now,
                heartbeat_at=now,
            )
            if rows:
                candidate.refresh_from_db()
                return candidate
            return None
        stale = (
            AuditJob.objects.filter(status=JobStatus.RUNNING, heartbeat_at__lt=cutoff)
            .order_by("heartbeat_at")
            .first()
        )
        if stale is None:
            return None
        rows = AuditJob.objects.filter(
            pk=stale.pk,
            status=JobStatus.RUNNING,
            heartbeat_at__lt=cutoff,
        ).update(worker_id=worker_id, leased_at=now, heartbeat_at=now)
        if not rows:
            return None
        stale.refresh_from_db()
        return stale


def heartbeat(job_id, worker_id: str) -> bool:
    """Refresh the lease. False means stop: cancelled, paused, or stolen."""
    now = timezone.now()
    rows = AuditJob.objects.filter(
        pk=job_id, worker_id=worker_id, status=JobStatus.RUNNING
    ).update(heartbeat_at=now)
    return rows > 0


def cancel_job(job_id) -> bool:
    """Cancel an in-flight job. The running worker notices on next check."""
    rows = AuditJob.objects.filter(pk=job_id, status__in=IN_FLIGHT).update(
        status=JobStatus.CANCELLED
    )
    return rows > 0


def _assert_writable(job: AuditJob, worker_id: str) -> None:
    if job.worker_id != worker_id:
        raise StaleWorkerError(f"job {job.pk} owned by {job.worker_id!r}")
    if job.status != JobStatus.RUNNING:
        raise JobNotRunningError(f"job {job.pk} is {job.status}")


def _check_caps(job: AuditJob) -> None:
    """Raise (without pausing) when a cap is already reached."""
    if job.calls_made >= job.max_calls:
        raise BudgetExceededError(
            f"job {job.pk} hit call cap",
            code="call_cap",
            detail=f"reached max calls ({job.max_calls})",
        )
    if job.spend_usd >= job.spend_cap_usd:
        raise BudgetExceededError(
            f"job {job.pk} hit spend cap",
            code="spend_cap",
            detail=f"reached spend cap (${job.spend_cap_usd})",
        )


def check_budget(job: AuditJob, worker_id: str) -> None:
    """Pre-call guard: ownership, liveness, call cap, spend cap.

    Pauses the job in autocommit before raising, so the halted state is
    durable. Must not run inside a write transaction that will roll back.
    """
    job.refresh_from_db()
    _assert_writable(job, worker_id)
    try:
        _check_caps(job)
    except BudgetExceededError as exc:
        _pause(job, exc.code, exc.detail)
        raise


def _pause(job: AuditJob, code: str, detail: str) -> None:
    AuditJob.objects.filter(pk=job.pk, status=JobStatus.RUNNING).update(
        status=JobStatus.PAUSED, error_code=code, error_detail=detail
    )
    job.refresh_from_db()


def record_execution(
    job_id,
    worker_id: str,
    *,
    stage: str,
    provider: str,
    model_id: str = "",
    photo_ids: list | None = None,
    ok: bool = True,
    latency_ms: int | None = None,
    usage: dict | None = None,
    cost_usd: Decimal | None = None,
    error_code: str = "",
    error_detail: str = "",
) -> AuditExecution:
    """Append a ledger row and charge it against the job budget.

    Unknown cost (``cost_usd=None`` on a provider call) pauses the job
    instead of treating the spend as zero. Local stages pass no cost and
    are never charged. Pauses and their raises happen after the ledger
    write commits, so the halted state is never rolled back.
    """
    from .models import Provider

    job = AuditJob.objects.get(pk=job_id)
    check_budget(job, worker_id)
    try:
        with transaction.atomic():
            job = AuditJob.objects.get(pk=job_id)
            _assert_writable(job, worker_id)
            _check_caps(job)
            execution = AuditExecution.objects.create(
                job=job,
                stage=stage,
                provider=provider,
                model_id=model_id,
                photo_ids=photo_ids or [],
                ok=ok,
                latency_ms=latency_ms,
                usage=usage or {},
                cost_usd=cost_usd,
                error_code=error_code,
                error_detail=error_detail,
            )
            AuditJob.objects.filter(pk=job.pk).update(calls_made=F("calls_made") + 1)
            if provider == Provider.LOCAL:
                return execution
            if cost_usd is None:
                AuditJob.objects.filter(pk=job.pk).update(
                    unknown_cost_calls=F("unknown_cost_calls") + 1
                )
            else:
                AuditJob.objects.filter(pk=job.pk).update(
                    spend_usd=F("spend_usd") + cost_usd
                )
    except BudgetExceededError as exc:
        _pause(job, exc.code, exc.detail)
        raise
    job.refresh_from_db()
    if provider != Provider.LOCAL and cost_usd is None:
        _pause(
            job,
            "unknown_cost",
            f"provider {provider} call has unknown cost; refusing to continue blind",
        )
        raise BudgetExceededError(
            f"job {job.pk} hit unknown cost",
            code="unknown_cost",
            detail="provider call has unknown cost",
        )
    if provider != Provider.LOCAL and job.spend_usd >= job.spend_cap_usd:
        _pause(
            job,
            "spend_cap",
            f"spend ${job.spend_usd} reached cap ${job.spend_cap_usd}",
        )
    return execution


def complete_job(job_id, worker_id: str) -> None:
    rows = AuditJob.objects.filter(
        pk=job_id, worker_id=worker_id, status=JobStatus.RUNNING
    ).update(status=JobStatus.COMPLETE)
    if not rows:
        raise StaleWorkerError(f"worker {worker_id} cannot complete {job_id}")


def fail_job(job_id, worker_id: str, code: str, detail: str = "") -> None:
    AuditJob.objects.filter(
        pk=job_id, worker_id=worker_id, status=JobStatus.RUNNING
    ).update(status=JobStatus.FAILED, error_code=code, error_detail=detail)
