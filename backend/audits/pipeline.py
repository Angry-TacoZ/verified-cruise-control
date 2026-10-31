"""Audit pipeline stages: screening (local) then Muse observation (live).

Prompt v1 (``PROMPT_VERSION = "m1"``). SAM, crop, recheck, and reconcile
stages plug into ``run_stages`` next; the job stays RUNNING until the full
pipeline completes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings

from evidence.images import original_path
from evidence.models import Photograph
from providers.base import ImageAttachment, ProviderError
from providers.baseten import BasetenMuseAdapter, MuseRequest

from . import services
from .models import (
    AuditJob,
    JobStatus,
    Observation,
    ObservationSource,
    Provider,
    Stage,
    Verdict,
)

PROMPT_VERSION = "m1"

SYSTEM_INSTRUCTIONS = (
    "You are an ADAS feature spotter for used-car listing audits. "
    "For each advertised claim and each photo, report exactly what the "
    "photo shows: never guess from the listing text, never verify a "
    "feature from one unclear view, and flag ambiguity explicitly. "
    "Respond with a JSON object containing an 'observations' array; each "
    "item has photo_id, claim_concept, visible_object (boolean), "
    "description, supports_localization (boolean), ambiguous (boolean), "
    "needs_crop (boolean), needs_other_view (boolean)."
)


def build_user_text(claims: list[str], photo_ids: list[str]) -> str:
    lines = [
        "Advertised claims:",
        *(f"- {claim}" for claim in claims),
        "",
        "Photos under review:",
        *(f"- {photo_id}" for photo_id in photo_ids),
        "",
        (
            "For every (claim, photo) pair, emit one observation. A photo may "
            "legitimately show nothing relevant; report that honestly."
        ),
    ]
    return "\n".join(lines)


@dataclass
class ScreenedJob:
    claims: list[str]
    photos: list[Photograph]


def load_images(photos: list[Photograph]) -> list[ImageAttachment]:
    attachments = []
    for photo in photos:
        data = original_path(photo.storage_ref, photo.image_format).read_bytes()
        attachments.append(
            ImageAttachment(
                photo_id=str(photo.id),
                mime_type=photo.mime_type,
                data=data,
            )
        )
    return attachments


def run_screening(job_id, worker_id: str) -> ScreenedJob:
    """Local pre-pass: claims + preserved originals must exist.

    Records a LOCAL ledger row (never charged). Fails the job with an
    exact blocker when there is nothing to audit.
    """
    job = AuditJob.objects.get(pk=job_id)
    services.check_budget(job, worker_id)
    claims = [str(c) for c in (job.listing.advertised_claims or []) if str(c)]
    photos = list(Photograph.objects.filter(listing=job.listing))
    if not claims:
        services.fail_job(job_id, worker_id, "no_claims", "listing has no claims")
        raise services.JobNotRunningError(f"job {job_id} has no claims")
    if not photos:
        services.fail_job(job_id, worker_id, "no_photos", "listing has no photos")
        raise services.JobNotRunningError(f"job {job_id} has no photos")
    services.record_execution(
        job_id,
        worker_id,
        stage=Stage.SCREENING,
        provider=Provider.LOCAL,
        photo_ids=[str(p.id) for p in photos],
        usage={"claims": len(claims), "photos": len(photos)},
    )
    return ScreenedJob(claims=claims, photos=photos)


def _verdict(item: dict) -> str:
    if item.get("ambiguous"):
        return Verdict.UNCLEAR
    if item.get("visible_object"):
        return Verdict.PRESENT
    return Verdict.ABSENT


def run_muse(
    job_id,
    worker_id: str,
    screened: ScreenedJob,
    adapter: BasetenMuseAdapter | None = None,
) -> int:
    """One Muse call over all photos; persist normalized observations.

    Idempotent on resume: existing (claim, photo, source) rows are kept.
    Returns the number of observations stored. Provider failures fail the
    job with the provider's error code.
    """
    job = AuditJob.objects.get(pk=job_id)
    services.check_budget(job, worker_id)
    own_adapter = adapter or BasetenMuseAdapter(
        model_id=settings.AUDIT_BASETEN_MODEL_ID
    )
    photo_ids = [str(p.id) for p in screened.photos]
    request = MuseRequest(
        prompt_version=PROMPT_VERSION,
        system_instructions=SYSTEM_INSTRUCTIONS,
        user_text=build_user_text(screened.claims, photo_ids),
        photo_ids=tuple(photo_ids),
        images=tuple(load_images(screened.photos)),
    )
    try:
        result = own_adapter.analyze(request)
    except ProviderError as exc:
        # Failed calls produce no inference, so they are not billed; the
        # call itself still counts against max_calls via record_execution.
        services.record_execution(
            job_id,
            worker_id,
            stage=Stage.Muse,
            provider=Provider.BASETEN,
            model_id=own_adapter.model_id,
            photo_ids=photo_ids,
            ok=False,
            usage={"failed_call_not_billed": True},
            cost_usd=Decimal("0.0000"),
            error_code=type(exc).__name__,
            error_detail=str(exc)[:500],
        )
        services.fail_job(job_id, worker_id, type(exc).__name__, str(exc)[:500])
        raise
    usage = result.usage
    cost = (
        Decimal(str(usage.estimated_cost_usd))
        if usage.estimated_cost_usd is not None
        else None
    )
    execution = services.record_execution(
        job_id,
        worker_id,
        stage=Stage.Muse,
        provider=Provider.BASETEN,
        model_id=result.model,
        photo_ids=list(usage.photo_ids),
        latency_ms=usage.latency_ms,
        usage={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "pricing_version": usage.pricing_version,
            "validation_warnings": list(result.validation_warnings),
        },
        cost_usd=cost,
    )
    by_id = {str(p.id): p for p in screened.photos}
    stored = 0
    for item in result.observations:
        photo = by_id.get(item["photo_id"])
        if photo is None:  # pragma: no cover - validator guarantees membership
            continue
        _, created = Observation.objects.get_or_create(
            job=job,
            claim_key=item["claim_concept"],
            photo=photo,
            source=ObservationSource.Muse,
            defaults={
                "verdict": _verdict(item),
                "rationale": item.get("description", ""),
                "execution": execution,
            },
        )
        if created:
            stored += 1
    return stored


def run_stages(
    job_id, worker_id: str, adapter: BasetenMuseAdapter | None = None
) -> AuditJob:
    """Run screening then Muse. Later stages extend this chain; the job is
    left RUNNING because reconcile (which completes it) is not built yet."""
    job = AuditJob.objects.get(pk=job_id)
    if job.status != JobStatus.RUNNING or job.worker_id != worker_id:
        raise services.StaleWorkerError(f"worker {worker_id} cannot run {job_id}")
    screened = run_screening(job_id, worker_id)
    if not services.heartbeat(job_id, worker_id):
        raise services.JobNotRunningError(f"job {job_id} stopped after screening")
    run_muse(job_id, worker_id, screened, adapter)
    services.heartbeat(job_id, worker_id)
    job.refresh_from_db()
    return job
