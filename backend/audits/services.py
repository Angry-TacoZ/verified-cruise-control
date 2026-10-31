"""Audit job lifecycle: queue (with reuse), atomic claim, heartbeat, cancel.

Budget rules fail closed: unknown cost pauses the job, and a stale worker
(differs from the recorded owner) can never write to it.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
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


def build_snapshot(listing_id) -> dict:
    """Freeze the audit inputs: claim IDs + texts, photo IDs + SHA-256.

    The pipeline processes this snapshot, never the live listing, so
    later claim edits or photo arrivals cannot shift historical findings.
    """
    from evidence.models import Photograph
    from listings.models import Listing

    listing = Listing.objects.get(pk=listing_id)
    claims = [str(c) for c in (listing.advertised_claims or []) if str(c)]
    photos = Photograph.objects.filter(listing=listing).order_by("created_at")
    return {
        "taken_at": timezone.now().isoformat(),
        "claims": [{"id": f"c{i + 1}", "text": text} for i, text in enumerate(claims)],
        "photos": [
            {
                "id": str(photo.pk),
                "sha256": photo.sha256,
                "width": photo.width,
                "height": photo.height,
                "mime_type": photo.mime_type,
                "image_format": photo.image_format,
                "storage_ref": photo.storage_ref,
            }
            for photo in photos
        ],
    }


def queue_audit(listing_id, requested_by: str = "") -> tuple[AuditJob, bool]:
    """Queue an audit, reusing an in-flight job for the same listing.

    Returns ``(job, created)``. Refuses to queue when inference is not
    explicitly enabled. The partial unique index is the backstop: a
    submission that loses a creation race returns the winner.
    """
    if not settings.AUDIT_INFERENCE_ENABLED:
        raise InferenceDisabledError(
            "Inference is disabled; set VCC_INFERENCE_ENABLED=1 to queue audits."
        )
    import random
    import time

    from django.db import OperationalError

    last_error: OperationalError | None = None
    for attempt in range(4):
        try:
            with transaction.atomic():
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
                    snapshot=build_snapshot(listing_id),
                    max_calls=settings.AUDIT_MAX_CALLS_PER_JOB,
                    spend_cap_usd=settings.AUDIT_SPEND_CAP_USD,
                )
                return job, True
        except IntegrityError:
            winner = (
                AuditJob.objects.filter(listing_id=listing_id, status__in=REUSABLE)
                .order_by("-created_at")
                .first()
            )
            if winner is None:
                raise
            return winner, False
        except OperationalError as exc:
            last_error = exc
            time.sleep(0.05 * (2**attempt) + random.uniform(0, 0.05))
    assert last_error is not None
    raise last_error


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
        # Reclaim resets any outstanding reservation: the previous
        # worker is presumed dead, and its in-flight call (if any) will
        # never settle. Unavoidable ambiguity: a call that completed
        # server-side microseconds before the crash may go unbooked, and
        # a late settle from a merely-slow worker still books its spend
        # without pausing another owner's job. No exactly-once billing
        # is claimed: providers offer no idempotency keys here.
        rows = AuditJob.objects.filter(
            pk=stale.pk,
            status=JobStatus.RUNNING,
            heartbeat_at__lt=cutoff,
        ).update(
            worker_id=worker_id,
            leased_at=now,
            heartbeat_at=now,
            reserved_calls=0,
            reserved_spend_usd=Decimal("0.0000"),
        )
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
    """Raise (without pausing) when a cap is already reached.

    Caps govern model calls only; local pipeline stages never consume
    the model allowance.
    """
    if job.model_calls_made >= job.max_calls:
        raise BudgetExceededError(
            f"job {job.pk} hit call cap",
            code="call_cap",
            detail=f"reached max model calls ({job.max_calls})",
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


def reserve_call(
    job_id, worker_id: str, *, max_attempts: int, reserve_usd: Decimal
) -> None:
    """Atomically reserve worst-case attempts + spend headroom for one
    provider call. The conditional UPDATE is the race guard: concurrent
    reservers converge on a single winner, and losers fail closed
    without pausing the job (a lost race is contention, not a cap hit).
    Cap breaches pause with an exact code. Must precede every provider
    call; every reservation is released by exactly one settle_call.
    """
    job = AuditJob.objects.get(pk=job_id)
    _assert_writable(job, worker_id)
    rows = AuditJob.objects.filter(
        pk=job.pk,
        status=JobStatus.RUNNING,
        worker_id=worker_id,
        reserved_calls=0,
        model_calls_made__lte=F("max_calls") - max_attempts,
        spend_usd__lte=F("spend_cap_usd") - reserve_usd,
    ).update(reserved_calls=max_attempts, reserved_spend_usd=reserve_usd)
    if rows:
        return
    job.refresh_from_db()
    _assert_writable(job, worker_id)
    if job.reserved_calls > 0:
        raise BudgetExceededError(
            f"job {job.pk} already has a provider call in flight",
            code="contention",
            detail="another reservation is outstanding",
        )
    if job.model_calls_made + max_attempts > job.max_calls:
        left = job.max_calls - job.model_calls_made
        _pause(
            job,
            "call_cap",
            f"next call needs {max_attempts} attempt(s); {left} left of {job.max_calls}",
        )
        raise BudgetExceededError(
            f"job {job.pk} hit call cap",
            code="call_cap",
            detail="next call would exceed the model-call allowance",
        )
    if job.spend_usd + reserve_usd > job.spend_cap_usd:
        _pause(
            job,
            "spend_cap",
            f"spend ${job.spend_usd} + ${reserve_usd} headroom exceeds cap "
            f"${job.spend_cap_usd}",
        )
        raise BudgetExceededError(
            f"job {job.pk} hit spend cap",
            code="spend_cap",
            detail="next call would exceed the spend cap",
        )
    raise BudgetExceededError(
        f"job {job.pk} lost a reservation race",
        code="contention",
        detail="another reserver won; retry the stage",
    )


def settle_call(
    job_id,
    worker_id: str,
    *,
    attempts: int,
    cost_usd: Decimal | None,
    ok: bool = True,
) -> AuditJob:
    """Book actual attempts + cost and release the reservation. Always
    books (spend is real even if the job was cancelled or reclaimed
    mid-call) but never overwrites a non-running status: pauses apply
    only to RUNNING jobs owned by the settler. The reservation is
    released only by its owner, so a stale worker's late settle can
    never zero a new owner's hold. Unknown cost pauses a running job
    instead of treating the spend as zero — unless ``ok`` is False,
    in which case the caller fails the job with the provider's code
    (the unknown spend stays visible in ``unknown_cost_calls`` and the
    failed execution row). Returns the refreshed job."""
    with transaction.atomic():
        AuditJob.objects.filter(pk=job_id).update(
            model_calls_made=F("model_calls_made") + attempts,
        )
        if cost_usd is None:
            AuditJob.objects.filter(pk=job_id).update(
                unknown_cost_calls=F("unknown_cost_calls") + 1
            )
        else:
            AuditJob.objects.filter(pk=job_id).update(
                spend_usd=F("spend_usd") + cost_usd
            )
        AuditJob.objects.filter(pk=job_id, worker_id=worker_id).update(
            reserved_calls=0,
            reserved_spend_usd=Decimal("0.0000"),
        )
    job = AuditJob.objects.get(pk=job_id)
    if job.status != JobStatus.RUNNING or job.worker_id != worker_id:
        return job
    if not ok:
        return job
    if cost_usd is None:
        _pause(
            job,
            "unknown_cost",
            "a provider call settled with unknown cost; refusing to continue blind",
        )
        return job
    if job.spend_usd >= job.spend_cap_usd:
        _pause(
            job,
            "spend_cap",
            f"spend ${job.spend_usd} reached cap ${job.spend_cap_usd}",
        )
    elif job.model_calls_made >= job.max_calls:
        _pause(
            job,
            "call_cap",
            f"reached max model calls ({job.max_calls})",
        )
    return job


def record_execution(
    job_id,
    worker_id: str,
    *,
    stage: str,
    provider: str,
    model_id: str = "",
    photo_ids: list | None = None,
    ok: bool = True,
    attempts: int = 1,
    latency_ms: int | None = None,
    usage: dict | None = None,
    cost_usd: Decimal | None = None,
    error_code: str = "",
    error_detail: str = "",
) -> AuditExecution:
    """Append a ledger row. Budget charging is separate (reserve before
    the call, settle after): rows are recorded even when the job was
    cancelled or reclaimed mid-call, because spend and attempts are real.
    Ownership is deliberately not asserted here; stages enforce liveness
    at batch boundaries, and observation writes are first-wins, so a
    stale worker can never overwrite another worker's results.
    """
    job = AuditJob.objects.get(pk=job_id)
    execution = AuditExecution.objects.create(
        job=job,
        stage=stage,
        provider=provider,
        model_id=model_id,
        photo_ids=photo_ids or [],
        ok=ok,
        attempts=attempts,
        latency_ms=latency_ms,
        usage=usage or {},
        cost_usd=cost_usd,
        error_code=error_code,
        error_detail=error_detail,
    )
    AuditJob.objects.filter(pk=job.pk).update(calls_made=F("calls_made") + 1)
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
