"""Audit pipeline stages: screening (local) then Muse observation (live).

Prompt v1 (``PROMPT_VERSION = "m1"``). SAM, crop, recheck, and reconcile
stages plug into ``run_stages`` next; the job stays RUNNING until the full
pipeline completes it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from PIL import Image

from evidence.images import original_path
from evidence.models import Photograph
from providers.base import ImageAttachment, ProviderError
from providers.baseten import BasetenMuseAdapter, MuseRequest
from providers.meta import MetaSamAdapter, SamRequest

from . import services
from .models import (
    AuditCrop,
    AuditFinding,
    AuditJob,
    EvidenceState,
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
            "prompt_version": usage.prompt_version,
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
                "extra": {
                    "supports_localization": bool(item.get("supports_localization")),
                    "needs_crop": bool(item.get("needs_crop")),
                    "needs_other_view": bool(item.get("needs_other_view")),
                },
                "execution": execution,
            },
        )
        if created:
            stored += 1
    return stored


CONCEPT_FORBIDDEN = set(".!?;<>|")


def audit_dirs(job_id) -> dict[str, Path]:
    """Derived-media dirs for one job (masks, crops). Originals untouched."""
    root = settings.RUNTIME_DIR / "audits" / str(job_id)
    dirs = {"root": root, "masks": root / "masks", "crops": root / "crops"}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def sanitize_concept(claim_key: str) -> str:
    """Reduce a claim to a SAM-legal short noun phrase, or ""."""
    cleaned = "".join(char for char in claim_key if char not in CONCEPT_FORBIDDEN)
    return " ".join(cleaned.split()[:10])


def write_mask_png(job_id, raster: bytes, width: int, height: int) -> str:
    """Persist one decoded mask raster; return its storage ref."""
    ref = uuid.uuid4().hex
    dest = audit_dirs(job_id)["masks"] / f"{ref}.png"
    tmp = dest.with_suffix(".tmp")
    Image.frombytes("L", (width, height), raster).save(tmp, format="PNG")
    tmp.replace(dest)
    return ref


def run_sam(
    job_id,
    worker_id: str,
    adapter: MetaSamAdapter | None = None,
) -> int:
    """Localize each PRESENT Muse observation flagged for it.

    One SAM call per (claim, photo) pair, skipped on resume when a SAM
    observation already exists. Zero matches is normal: it persists as an
    ABSENT observation so reconcile can weigh it against Muse. Returns the
    number of pairs localized.
    """
    job = AuditJob.objects.get(pk=job_id)
    services.check_budget(job, worker_id)
    own_adapter = adapter or MetaSamAdapter(model_id=settings.AUDIT_SAM_MODEL_ID)
    targets = Observation.objects.filter(
        job=job, source=ObservationSource.Muse, verdict=Verdict.PRESENT
    )
    done = set(
        Observation.objects.filter(job=job, source=ObservationSource.SAM).values_list(
            "claim_key", "photo_id"
        )
    )
    localized = 0
    for target in targets.order_by("claim_key", "photo_id"):
        if (target.claim_key, target.photo_id) in done:
            continue
        if not services.heartbeat(job_id, worker_id):
            raise services.JobNotRunningError(f"job {job_id} stopped during sam")
        if not target.extra.get("supports_localization"):
            continue
        concept = sanitize_concept(target.claim_key)
        photo = target.photo
        if not concept:
            Observation.objects.create(
                job=job,
                claim_key=target.claim_key,
                photo=photo,
                source=ObservationSource.SAM,
                verdict=Verdict.UNCLEAR,
                rationale="claim yields no SAM concept phrase",
            )
            continue
        data = original_path(photo.storage_ref, photo.image_format).read_bytes()
        request = SamRequest(
            photo=ImageAttachment(
                photo_id=str(photo.id), mime_type=photo.mime_type, data=data
            ),
            image_width=photo.width,
            image_height=photo.height,
            concept=concept,
        )
        try:
            result = own_adapter.analyze(request)
        except ProviderError as exc:
            services.record_execution(
                job_id,
                worker_id,
                stage=Stage.SAM,
                provider=Provider.META,
                model_id=own_adapter.model_id,
                photo_ids=[str(photo.id)],
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
            stage=Stage.SAM,
            provider=Provider.META,
            model_id=result.model,
            photo_ids=[str(photo.id)],
            latency_ms=usage.latency_ms,
            usage={
                "pricing_version": usage.pricing_version,
                "warnings": list(result.warnings),
            },
            cost_usd=cost,
        )
        boxes = []
        for record in result.records:
            x1, y1, x2, y2 = record.box
            boxes.append(
                {
                    "box": [x1, y1, x2, y2],
                    "mask_ref": write_mask_png(
                        job_id, record.raster, record.width, record.height
                    ),
                    "mask_size": [record.width, record.height],
                    "confidence": record.provider_confidence,
                }
            )
        Observation.objects.create(
            job=job,
            claim_key=target.claim_key,
            photo=photo,
            source=ObservationSource.SAM,
            verdict=Verdict.PRESENT if boxes else Verdict.ABSENT,
            boxes=boxes,
            rationale=(
                f"{len(boxes)} segment(s) for concept {concept!r}"
                if boxes
                else f"no segments for concept {concept!r}"
            ),
            execution=execution,
        )
        localized += 1
    return localized


def run_crop(job_id, worker_id: str) -> int:
    """Cut box crops from PRESENT SAM observations, bounded by the crop cap.

    Local and deterministic: boxes already cropped are skipped on resume by
    comparing (photo, box). Boxes are clamped to the original; degenerate
    boxes are skipped, never fabricated. Returns crops created this run.
    """
    import json

    job = AuditJob.objects.get(pk=job_id)
    services.check_budget(job, worker_id)
    cap = settings.AUDIT_MAX_CROPS_PER_JOB
    existing = {
        (str(c.source_photo_id), json.dumps(c.box, sort_keys=True))
        for c in AuditCrop.objects.filter(job=job)
    }
    made = 0
    skipped = 0
    observations = Observation.objects.filter(
        job=job, source=ObservationSource.SAM, verdict=Verdict.PRESENT
    ).order_by("claim_key", "photo_id")
    for obs in observations:
        photo = obs.photo
        with Image.open(original_path(photo.storage_ref, photo.image_format)) as img:
            width, height = img.size
            for entry in obs.boxes:
                key = (
                    str(photo.pk),
                    json.dumps(entry.get("box"), sort_keys=True),
                )
                if key in existing:
                    continue
                x1, y1, x2, y2 = entry["box"]
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(width, x2), min(height, y2)
                if x2 <= x1 or y2 <= y1:
                    skipped += 1
                    continue
                if AuditCrop.objects.filter(job=job).count() >= cap:
                    skipped += 1
                    continue
                ref = uuid.uuid4().hex
                dest = audit_dirs(job_id)["crops"] / f"{ref}.png"
                tmp = dest.with_suffix(".tmp")
                img.crop((x1, y1, x2, y2)).save(tmp, format="PNG")
                tmp.replace(dest)
                AuditCrop.objects.create(
                    job=job,
                    source_photo=photo,
                    box=entry["box"],
                    storage_ref=ref,
                    mask_storage_ref=entry.get("mask_ref"),
                    width=x2 - x1,
                    height=y2 - y1,
                )
                existing.add(key)
                made += 1
        if not services.heartbeat(job_id, worker_id):
            raise services.JobNotRunningError(f"job {job_id} stopped during crop")
    services.record_execution(
        job_id,
        worker_id,
        stage=Stage.CROP,
        provider=Provider.LOCAL,
        usage={"crops_made": made, "skipped": skipped, "cap": cap},
    )
    return made


RECHECK_PROMPT_VERSION = "m1-recheck"

RECHECK_SYSTEM_INSTRUCTIONS = (
    "You are an ADAS feature spotter for used-car listing audits. "
    "The attached image is a zoomed crop cut from the listed photo to "
    "inspect one region closely. Report exactly what the crop shows for "
    "each advertised claim: never guess from the listing text, and flag "
    "ambiguity explicitly. Respond with a JSON object containing an "
    "'observations' array; each item has photo_id (the source photo), "
    "claim_concept, visible_object (boolean), description, "
    "supports_localization (boolean), ambiguous (boolean), needs_crop "
    "(boolean), needs_other_view (boolean)."
)


def run_recheck(
    job_id,
    worker_id: str,
    adapter: BasetenMuseAdapter | None = None,
) -> int:
    """Re-examine crops with Muse, one call per crop, bounded by the
    recheck cap. A crop is skipped when every claim already has a RECHECK
    observation for its photo; the first crop wins each (claim, photo)
    pair. Returns provider calls made this run."""
    job = AuditJob.objects.get(pk=job_id)
    services.check_budget(job, worker_id)
    own_adapter = adapter or BasetenMuseAdapter(
        model_id=settings.AUDIT_BASETEN_MODEL_ID
    )
    cap = settings.AUDIT_MAX_RECHECKS_PER_JOB
    claims = [str(c) for c in (job.listing.advertised_claims or []) if str(c)]
    covered = set(
        Observation.objects.filter(
            job=job, source=ObservationSource.RECHECK
        ).values_list("claim_key", "photo_id")
    )
    calls = 0
    crops_dir = audit_dirs(job_id)["crops"]
    for crop in AuditCrop.objects.filter(job=job).order_by("created_at"):
        if calls >= cap:
            break
        photo = crop.source_photo
        if all((claim, photo.pk) in covered for claim in claims):
            continue
        if not services.heartbeat(job_id, worker_id):
            raise services.JobNotRunningError(f"job {job_id} stopped during recheck")
        data = (crops_dir / f"{crop.storage_ref}.png").read_bytes()
        request = MuseRequest(
            prompt_version=RECHECK_PROMPT_VERSION,
            system_instructions=RECHECK_SYSTEM_INSTRUCTIONS,
            user_text=(
                build_user_text(claims, [str(photo.id)])
                + "\n\nThe attached image is a zoomed crop from photo "
                f"{photo.id}; judge only what the crop shows."
            ),
            photo_ids=(str(photo.id),),
            images=(
                ImageAttachment(
                    photo_id=str(photo.id), mime_type="image/png", data=data
                ),
            ),
        )
        try:
            result = own_adapter.analyze(request)
        except ProviderError as exc:
            services.record_execution(
                job_id,
                worker_id,
                stage=Stage.RECHECK,
                provider=Provider.BASETEN,
                model_id=own_adapter.model_id,
                photo_ids=[str(photo.id)],
                ok=False,
                usage={"failed_call_not_billed": True},
                cost_usd=Decimal("0.0000"),
                error_code=type(exc).__name__,
                error_detail=str(exc)[:500],
            )
            services.fail_job(job_id, worker_id, type(exc).__name__, str(exc)[:500])
            raise
        calls += 1
        usage = result.usage
        cost = (
            Decimal(str(usage.estimated_cost_usd))
            if usage.estimated_cost_usd is not None
            else None
        )
        execution = services.record_execution(
            job_id,
            worker_id,
            stage=Stage.RECHECK,
            provider=Provider.BASETEN,
            model_id=result.model,
            photo_ids=[str(photo.id)],
            latency_ms=usage.latency_ms,
            usage={
                "prompt_version": usage.prompt_version,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "pricing_version": usage.pricing_version,
                "crop_ref": crop.storage_ref,
            },
            cost_usd=cost,
        )
        for item in result.observations:
            if item["photo_id"] != str(photo.id):
                continue
            _, created = Observation.objects.get_or_create(
                job=job,
                claim_key=item["claim_concept"],
                photo=photo,
                source=ObservationSource.RECHECK,
                defaults={
                    "verdict": _verdict(item),
                    "rationale": item.get("description", ""),
                    "extra": {"crop_ref": crop.storage_ref},
                    "execution": execution,
                },
            )
            if created:
                covered.add((item["claim_concept"], photo.pk))
    return calls


def reconcile_claim(observations: list[Observation]) -> tuple[str, str]:
    """Deterministic five-state rule for one claim (pure, testable).

    VERIFIED is never assigned: photos alone cannot establish a feature,
    authoritative documentation would be required. Returns (state, summary).
    """
    by_source: dict[str, list[Observation]] = {}
    for obs in observations:
        by_source.setdefault(obs.source, []).append(obs)
    muse = by_source.get(ObservationSource.Muse, [])
    present_sources = {
        obs.source for obs in observations if obs.verdict == Verdict.PRESENT
    }
    any_present = bool(present_sources)
    muse_absent_all = bool(muse) and all(o.verdict == Verdict.ABSENT for o in muse)
    all_muse_unclear = bool(muse) and all(o.verdict == Verdict.UNCLEAR for o in muse)
    needs_other_view = any(o.extra.get("needs_other_view") for o in muse)
    n_photos = len({o.photo_id for o in muse})

    if muse_absent_all and not any_present:
        return (
            EvidenceState.CONTRADICTED,
            f"feature absent in all {n_photos} photo(s); no source saw it",
        )
    if ObservationSource.SAM in present_sources and (
        ObservationSource.Muse in present_sources
    ):
        return (
            EvidenceState.VISUALLY_SUPPORTED,
            "Muse sighting confirmed by SAM localization",
        )
    if ObservationSource.RECHECK in present_sources:
        return (
            EvidenceState.VISUALLY_SUPPORTED,
            "crop recheck confirmed the feature",
        )
    if all_muse_unclear and needs_other_view and not any_present:
        return (
            EvidenceState.NOT_VISUALLY_VERIFIABLE,
            "photos cannot resolve this; another evidence form is needed",
        )
    return (
        EvidenceState.INSUFFICIENT,
        "signals disagree or are too weak to decide",
    )


def run_reconcile(job_id, worker_id: str) -> int:
    """Apply the deterministic rules per claim, persist findings, complete
    the job. Reruns overwrite findings (update_or_create), never duplicate."""
    job = AuditJob.objects.get(pk=job_id)
    services.check_budget(job, worker_id)
    claims = [str(c) for c in (job.listing.advertised_claims or []) if str(c)]
    count = 0
    for claim in claims:
        observations = list(
            Observation.objects.filter(job=job, claim_key=claim).order_by("created_at")
        )
        state, summary = reconcile_claim(observations)
        AuditFinding.objects.update_or_create(
            job=job,
            claim_key=claim,
            defaults={
                "state": state,
                "summary": summary,
                "evidence": {
                    "rule": "v1",
                    "observations": [
                        {
                            "id": str(o.id),
                            "source": o.source,
                            "photo_id": str(o.photo_id),
                            "verdict": o.verdict,
                        }
                        for o in observations
                    ],
                },
            },
        )
        count += 1
    services.record_execution(
        job_id,
        worker_id,
        stage=Stage.RECONCILE,
        provider=Provider.LOCAL,
        usage={"findings": count},
    )
    services.complete_job(job_id, worker_id)
    return count


def run_stages(
    job_id,
    worker_id: str,
    muse_adapter: BasetenMuseAdapter | None = None,
    sam_adapter: MetaSamAdapter | None = None,
) -> AuditJob:
    """Run the full pipeline: screening, Muse, SAM, crop, recheck,
    reconcile. Heartbeats between stages let cancel win promptly."""
    job = AuditJob.objects.get(pk=job_id)
    if job.status != JobStatus.RUNNING or job.worker_id != worker_id:
        raise services.StaleWorkerError(f"worker {worker_id} cannot run {job_id}")
    screened = run_screening(job_id, worker_id)
    if not services.heartbeat(job_id, worker_id):
        raise services.JobNotRunningError(f"job {job_id} stopped after screening")
    run_muse(job_id, worker_id, screened, muse_adapter)
    if not services.heartbeat(job_id, worker_id):
        raise services.JobNotRunningError(f"job {job_id} stopped after muse")
    run_sam(job_id, worker_id, sam_adapter)
    if not services.heartbeat(job_id, worker_id):
        raise services.JobNotRunningError(f"job {job_id} stopped after sam")
    run_crop(job_id, worker_id)
    if not services.heartbeat(job_id, worker_id):
        raise services.JobNotRunningError(f"job {job_id} stopped after crop")
    run_recheck(job_id, worker_id, muse_adapter)
    if not services.heartbeat(job_id, worker_id):
        raise services.JobNotRunningError(f"job {job_id} stopped after recheck")
    run_reconcile(job_id, worker_id)
    job.refresh_from_db()
    return job
