"""Audit pipeline stages: screening, Muse, SAM, crop, recheck, reconcile.

Prompt v2 (``PROMPT_VERSION = "m2"``): Muse identifies physical ADAS
controls (symbol, type, location, dedicated vs menu) for manufacturer-aware
interpretation; SAM localizes the observed object; deterministic
reconciliation maps evidence to the five-state taxonomy.
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import transaction
from PIL import Image

from evidence.images import original_path
from evidence.models import Photograph
from providers.base import (
    ImageAttachment,
    ProviderAuthError,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimitError,
    ProviderResponseError,
    RetryPolicy,
)
from providers.baseten import BasetenMuseAdapter, MuseRequest
from providers.meta import MetaSamAdapter, SamRequest

from . import adas_evidence, services
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

PROMPT_VERSION = "m2"

SYSTEM_INSTRUCTIONS = (
    "You are an ADAS feature spotter for used-car listing audits. "
    "For each advertised claim and each photo, report exactly what the "
    "photo shows: never guess from the listing text, never label a "
    "feature present merely because the claim suggests it should exist, "
    "never verify a feature from one unclear view, and flag ambiguity "
    "explicitly. ADAS controls vary by manufacturer, model, year, and "
    "trim: describe the physical control you see, not the capability "
    "you assume. "
    "Respond with a JSON object containing an 'observations' array; each "
    "item has photo_id, claim_concept (the claim ID, e.g. c1), "
    "visible_object (a short string naming the visible object, or the "
    'literal string "none"), description, supports_localization '
    "(boolean), ambiguous (boolean), needs_crop (boolean), needs_other_view "
    "(boolean). "
    "When a control is visible, also report: control_symbol (the exact "
    "icon or markings, e.g. 'vehicle between two lane lines'), "
    "control_type (button, stalk button, menu entry, cluster indicator, "
    "sensor housing, camera housing, or similar), location (one of: "
    "steering_wheel, left_dashboard, stalk, instrument_cluster, "
    "infotainment, center_console, exterior_sensor, windshield_camera, "
    "unknown), dedicated (true only when the control clearly serves one "
    "feature rather than opening a general menu), and "
    "manufacturer_context (visible make/model cues, if any). "
    "If a symbol is unreadable, partially obscured, or unrecognized, say "
    "so in plain words instead of guessing. "
    "If one photo shows several distinct ADAS controls relevant to the "
    "same claim, list each under 'controls' (at most 4, each with "
    "visible_object plus symbol/type/location as visible) instead of "
    "merging them into one generic observation."
)

# Batching limits (documented, tested): one photo per Muse call, at most
# six claims per call, 4096 output tokens. Six observations were
# live-verified to fit in 4096 tokens (evals 2026-10-30-muse-m1); larger
# claim sets are chunked so no call can silently truncate.
MUSE_MAX_CLAIMS_PER_CALL = 6
MUSE_MAX_OUTPUT_TOKENS = 4096


def chunked(items: list, size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def build_user_text(entries: list[dict], photo_ids: list[str]) -> str:
    lines = [
        "Advertised claims (ID: text):",
        *(f"- {entry['id']}: {entry['text']}" for entry in entries),
        "",
        "Photos under review:",
        *(f"- {photo_id}" for photo_id in photo_ids),
        "",
        (
            "For every (claim ID, photo) pair, emit one observation with "
            "claim_concept set to the claim ID. A photo may legitimately "
            "show nothing relevant; report that honestly."
        ),
    ]
    return "\n".join(lines)


@dataclass
class ScreenedJob:
    claims: list[str]
    photos: list[Photograph]


def verified_original_bytes(job_id, worker_id: str, photo: Photograph) -> bytes:
    """Read an original's bytes, proving they still match the snapshot.

    The snapshot SHA-256 is the frozen truth: bytes that no longer match
    fail the job with ``evidence_tampered`` before any inference can use
    them. Never repairs or re-points; the snapshot is never rewritten.
    """
    job = AuditJob.objects.get(pk=job_id)
    entries = {entry.get("id"): entry for entry in get_snapshot(job).get("photos", [])}
    entry = entries.get(str(photo.pk))
    if entry is None or not entry.get("sha256"):
        detail = f"photo {photo.pk} has no snapshotted hash"
        services.fail_job(job_id, worker_id, "evidence_missing", detail)
        raise services.JobNotRunningError(f"job {job_id} lost evidence")
    try:
        data = original_path(photo.storage_ref, photo.image_format).read_bytes()
    except OSError:
        detail = f"snapshotted original {photo.pk} unreadable from storage"
        services.fail_job(job_id, worker_id, "evidence_missing", detail)
        raise services.JobNotRunningError(f"job {job_id} lost evidence")
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        detail = f"original {photo.pk} no longer matches its snapshot SHA-256"
        services.fail_job(job_id, worker_id, "evidence_tampered", detail)
        raise services.JobNotRunningError(f"job {job_id} evidence tampered")
    return data


def load_images(
    job_id, worker_id: str, photos: list[Photograph]
) -> list[ImageAttachment]:
    attachments = []
    for photo in photos:
        data = verified_original_bytes(job_id, worker_id, photo)
        attachments.append(
            ImageAttachment(
                photo_id=str(photo.id),
                mime_type=photo.mime_type,
                data=data,
            )
        )
    return attachments


def get_snapshot(job: AuditJob) -> dict:
    """This job's frozen inputs. Rows created before snapshots existed
    (dev databases only) are backfilled once from the live listing."""
    if job.snapshot:
        return job.snapshot
    snapshot = services.build_snapshot(job.listing_id)
    AuditJob.objects.filter(pk=job.pk).update(snapshot=snapshot)
    job.snapshot = snapshot
    return snapshot


_SNAPSHOT_META_FIELDS = (
    "sha256",
    "width",
    "height",
    "mime_type",
    "image_format",
    "storage_ref",
)


def run_screening(job_id, worker_id: str) -> ScreenedJob:
    """Local pre-pass: the snapshot's claims + preserved originals must
    exist, match their snapshotted metadata, and hash to their snapshotted
    SHA-256.

    Records a LOCAL ledger row (never charged). Fails the job with an
    exact blocker when there is nothing to audit, snapshotted evidence
    went missing, metadata drifted, or bytes no longer hash-match (never
    silently proceeds on a subset, and never rewrites the snapshot).
    Runs at the start of every ``run_stages`` pass, so a worker restart
    re-verifies before any new provider call.
    """
    job = AuditJob.objects.get(pk=job_id)
    services.check_budget(job, worker_id)
    snapshot = get_snapshot(job)
    claims = [c["text"] for c in snapshot.get("claims", []) if c.get("text")]
    entries = {p["id"]: p for p in snapshot.get("photos", [])}
    wanted = list(entries)
    if not claims:
        services.fail_job(job_id, worker_id, "no_claims", "listing has no claims")
        raise services.JobNotRunningError(f"job {job_id} has no claims")
    if not wanted:
        services.fail_job(job_id, worker_id, "no_photos", "listing has no photos")
        raise services.JobNotRunningError(f"job {job_id} has no photos")
    rows = {str(p.pk): p for p in Photograph.objects.filter(pk__in=wanted)}
    missing = [photo_id for photo_id in wanted if photo_id not in rows]
    if missing:
        detail = f"{len(missing)} snapshotted photo(s) missing from the database"
        services.fail_job(job_id, worker_id, "evidence_missing", detail)
        raise services.JobNotRunningError(f"job {job_id} lost evidence")
    photos = [rows[photo_id] for photo_id in wanted]
    drifted = [
        str(p.pk)
        for p in photos
        if any(
            getattr(p, field) != entries[str(p.pk)].get(field)
            for field in _SNAPSHOT_META_FIELDS
        )
    ]
    if drifted:
        detail = (
            f"{len(drifted)} photo(s) no longer match snapshot metadata: "
            + ", ".join(sorted(drifted)[:3])
        )
        services.fail_job(job_id, worker_id, "evidence_metadata_drift", detail)
        raise services.JobNotRunningError(f"job {job_id} evidence drifted")
    gone = [
        str(p.pk)
        for p in photos
        if not original_path(p.storage_ref, p.image_format).is_file()
    ]
    if gone:
        detail = f"{len(gone)} snapshotted original(s) missing from storage"
        services.fail_job(job_id, worker_id, "evidence_missing", detail)
        raise services.JobNotRunningError(f"job {job_id} lost evidence")
    for photo in photos:
        data = original_path(photo.storage_ref, photo.image_format).read_bytes()
        if hashlib.sha256(data).hexdigest() != entries[str(photo.pk)]["sha256"]:
            detail = f"original {photo.pk} no longer matches its snapshot SHA-256"
            services.fail_job(job_id, worker_id, "evidence_tampered", detail)
            raise services.JobNotRunningError(f"job {job_id} evidence tampered")
    services.record_execution(
        job_id,
        worker_id,
        stage=Stage.SCREENING,
        provider=Provider.LOCAL,
        photo_ids=[str(p.id) for p in photos],
        usage={
            "claims": len(claims),
            "photos": len(photos),
            "integrity": "sha256-verified",
        },
    )
    return ScreenedJob(claims=claims, photos=photos)


def classify_error_cost(exc: ProviderError) -> Decimal | None:
    """Settle a failed provider call: $0.00 only when the provider
    certainly ran no inference (never-sent calls, auth/rate-limit and
    other 4xx rejections short of timeouts); None (unknown, fail
    closed) for timeouts, 5xx, and 200-malformed responses whose
    metering never surfaced."""

    if isinstance(
        exc, (ProviderAuthError, ProviderRateLimitError, ProviderNotConfigured)
    ):
        return Decimal("0.0000")
    status = getattr(exc, "status", None)
    if isinstance(status, int) and 400 <= status < 500 and status != 408:
        return Decimal("0.0000")
    return None


def _max_attempts_for(adapter) -> int:
    policy = getattr(adapter, "retry_policy", None)
    if policy is None:
        policy = RetryPolicy()
    return policy.max_retries + 1


def _error_attempts(adapter, exc: ProviderError, max_attempts: int) -> int:
    if isinstance(exc, ProviderNotConfigured):
        return 0
    return getattr(adapter, "last_attempts", 0) or max_attempts


def _guarded_analyze(
    job_id,
    worker_id: str,
    adapter,
    request,
    *,
    stage: str,
    provider: str,
    model_id: str,
    photo_ids: list[str],
):
    """Reserve budget, run one provider call, settle actuals, record the
    error row on failure. Returns ``(result, attempts, cost, settled_job)``.
    Budget pauses raise before any request is sent; provider failures fail
    the job with the provider's code after settling.
    """

    max_attempts = _max_attempts_for(adapter)
    services.reserve_call(
        job_id,
        worker_id,
        max_attempts=max_attempts,
        reserve_usd=settings.AUDIT_RESERVE_PER_CALL_USD,
    )
    try:
        result = adapter.analyze(request)
    except ProviderError as exc:
        attempts = _error_attempts(adapter, exc, max_attempts)
        cost = classify_error_cost(exc)
        services.settle_call(
            job_id, worker_id, attempts=attempts, cost_usd=cost, ok=False
        )
        services.record_execution(
            job_id,
            worker_id,
            stage=stage,
            provider=provider,
            model_id=model_id,
            photo_ids=photo_ids,
            ok=False,
            attempts=attempts,
            usage={"billing": "certain-zero" if cost is not None else "unknown"},
            cost_usd=cost,
            error_code=type(exc).__name__,
            error_detail=str(exc)[:500],
        )
        services.fail_job(job_id, worker_id, type(exc).__name__, str(exc)[:500])
        raise
    attempts = (result.usage.retries or 0) + 1
    estimated = result.usage.estimated_cost_usd
    cost = Decimal(str(estimated)) if estimated is not None else None
    settled = services.settle_call(job_id, worker_id, attempts=attempts, cost_usd=cost)
    return result, attempts, cost, settled


_BUDGET_HALT_CODES = frozenset({"call_cap", "spend_cap", "unknown_cost"})


def _raise_if_halted(settled, job_id, stage: str, worker_id: str) -> None:
    """Halt the stage when settle left the job non-running or re-owned.

    Budget pauses surface as ``BudgetExceededError``; external stops
    (cancel) surface as ``JobNotRunningError``; a job reclaimed by
    another worker surfaces as ``StaleWorkerError``. The worker treats
    all three as graceful halts; the distinction keeps dashboards
    and retry policy honest about *why* the audit stopped.
    """
    if settled.status == JobStatus.RUNNING and settled.worker_id == worker_id:
        return
    if settled.worker_id != worker_id:
        raise services.StaleWorkerError(
            f"worker {worker_id} lost job {job_id} during {stage}"
        )
    if settled.error_code in _BUDGET_HALT_CODES:
        raise services.BudgetExceededError(f"job {job_id} halted during {stage} settle")
    raise services.JobNotRunningError(f"job {job_id} stopped during {stage}")


def _verdict(item: dict) -> str:
    """Map a validated observation to PRESENT/ABSENT/UNCLEAR.

    The validator contract makes ``visible_object`` a descriptive string;
    the model writes the literal ``"none"`` when nothing relevant is
    visible (spelled out in the prompt, mirroring the smoke prompt).
    """
    if item.get("ambiguous"):
        return Verdict.UNCLEAR
    if str(item.get("visible_object", "")).strip().lower() == "none":
        return Verdict.ABSENT
    return Verdict.PRESENT


_CONTROL_EXTRA_KEYS = (
    "visible_object",
    "control_symbol",
    "control_type",
    "location",
    "dedicated",
    "manufacturer_context",
    "controls",
)

_MUSE_FAMILY = (ObservationSource.Muse, ObservationSource.RECHECK)

_CATALOG: adas_evidence.Catalog | None = None


def get_catalog() -> adas_evidence.Catalog:
    """Process-wide validated ADAS reference catalog (loaded once)."""
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = adas_evidence.load_catalog()
    return _CATALOG


def _control_extra(item: dict) -> dict:
    """Carry Layer B control identification into the observation row.

    Only keys the validator accepted are copied (all already capped);
    absent optionals stay absent so legacy-shaped items are unchanged.
    """
    return {key: item[key] for key in _CONTROL_EXTRA_KEYS if key in item}


def validate_batch(
    items, expected: set[tuple[str, str]], done: set[tuple[str, str]]
) -> tuple[dict, list[str], tuple[str, str] | None]:
    """Check one batch response against its expected (claim ID, photo) set.

    Returns ``(seen, warnings, fatal)`` where ``seen`` maps pairs to items
    (first duplicate wins, with a warning) and ``fatal`` is a
    ``(code, detail)`` pair when the batch must fail the job instead of
    silently producing partial evidence.
    """
    batch_ids = {cid for cid, _ in expected}
    batch_photos = {pid for _, pid in expected}
    seen: dict[tuple[str, str], dict] = {}
    warnings: list[str] = []
    for index, item in enumerate(items):
        cid = item.get("claim_concept")
        pid = item.get("photo_id")
        if cid not in batch_ids:
            return {}, [], ("unknown_claim", f"observation has unknown claim {cid!r}")
        if pid not in batch_photos:
            return (
                {},
                [],
                ("unknown_photo", f"observation has unexpected photo {pid!r}"),
            )
        if (cid, pid) in seen:
            warnings.append(f"duplicate observation for {(cid, pid)}; first wins")
            continue
        seen[(cid, pid)] = item
    outstanding = expected - done
    if not items and outstanding:
        return (
            {},
            [],
            (
                "empty_batch",
                f"model returned no observations for {len(outstanding)} pair(s)",
            ),
        )
    missing = expected - set(seen) - done
    if missing:
        sample = sorted(missing)[:3]
        return (
            {},
            [],
            (
                "incomplete_batch",
                f"model omitted {len(missing)} pair(s), e.g. {sample}",
            ),
        )
    return seen, warnings, None


def run_muse(
    job_id,
    worker_id: str,
    screened: ScreenedJob,
    adapter: BasetenMuseAdapter | None = None,
) -> int:
    """Batched Muse observation: one photo and up to six claims per call.

    Batches already complete in the database are skipped, so worker
    restarts never repeat paid calls. Every batch response is checked for
    completeness against its expected pairs: unknown IDs, missing pairs,
    or empty responses fail the job with an exact code instead of
    silently producing partial evidence. Returns observations stored.
    """
    job = AuditJob.objects.get(pk=job_id)
    services.check_budget(job, worker_id)
    own_adapter = adapter or BasetenMuseAdapter(
        model_id=settings.AUDIT_BASETEN_MODEL_ID
    )
    snapshot = get_snapshot(job)
    entries = snapshot.get("claims", [])
    done = {
        (claim_key, str(photo_id))
        for claim_key, photo_id in Observation.objects.filter(
            job=job, source=ObservationSource.Muse
        ).values_list("claim_key", "photo_id")
    }
    stored = 0
    images = {str(p.id): p for p in screened.photos}
    for photo in screened.photos:
        photo_attachments = load_images(job_id, worker_id, [photo])
        for chunk in chunked(entries, MUSE_MAX_CLAIMS_PER_CALL):
            expected = {(entry["id"], str(photo.pk)) for entry in chunk}
            if expected <= done:
                continue
            if not services.heartbeat(job_id, worker_id):
                raise services.JobNotRunningError(f"job {job_id} stopped during muse")
            request = MuseRequest(
                prompt_version=PROMPT_VERSION,
                system_instructions=SYSTEM_INSTRUCTIONS,
                user_text=build_user_text(chunk, [str(photo.pk)]),
                photo_ids=(str(photo.pk),),
                images=tuple(photo_attachments),
                max_output_tokens=MUSE_MAX_OUTPUT_TOKENS,
            )
            result, attempts, cost, settled = _guarded_analyze(
                job_id,
                worker_id,
                own_adapter,
                request,
                stage=Stage.Muse,
                provider=Provider.BASETEN,
                model_id=own_adapter.model_id,
                photo_ids=[str(photo.pk)],
            )
            usage = result.usage
            seen, warnings, fatal = validate_batch(result.observations, expected, done)
            execution = services.record_execution(
                job_id,
                worker_id,
                stage=Stage.Muse,
                provider=Provider.BASETEN,
                model_id=result.model,
                photo_ids=[str(photo.pk)],
                attempts=attempts,
                latency_ms=usage.latency_ms,
                usage={
                    "prompt_version": usage.prompt_version,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "pricing_version": usage.pricing_version,
                    "validation_warnings": list(result.validation_warnings) + warnings,
                },
                cost_usd=cost,
            )
            if fatal is not None:
                code, detail = fatal
                services.fail_job(job_id, worker_id, code, detail)
                raise ProviderResponseError(detail)
            with transaction.atomic():
                # Ownership fence: a worker that lost the lease while the
                # call was in flight keeps its ledger row (spend is real)
                # but contributes no accepted evidence.
                services.fence_evidence_write(job_id, worker_id)
                for (cid, _), item in seen.items():
                    _, created = Observation.objects.get_or_create(
                        job=job,
                        claim_key=cid,
                        photo=images[str(photo.pk)],
                        source=ObservationSource.Muse,
                        defaults={
                            "verdict": _verdict(item),
                            "rationale": item.get("description", ""),
                            "extra": {
                                "supports_localization": bool(
                                    item.get("supports_localization")
                                ),
                                "needs_crop": bool(item.get("needs_crop")),
                                "needs_other_view": bool(item.get("needs_other_view")),
                                **_control_extra(item),
                            },
                            "execution": execution,
                        },
                    )
                    if created:
                        stored += 1
                    done.add((cid, str(photo.pk)))
            # Spend for this batch is already booked; the observations
            # above are kept so a resumed job never re-pays for them.
            _raise_if_halted(settled, job_id, Stage.Muse, worker_id)
    return stored


CONCEPT_FORBIDDEN = set(".!?;<>|")


def audit_dirs(job_id) -> dict[str, Path]:
    """Derived-media dirs for one job (masks, crops). Originals untouched."""
    root = settings.RUNTIME_DIR / "audits" / str(job_id)
    dirs = {"root": root, "masks": root / "masks", "crops": root / "crops"}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def sanitize_concept(text: str) -> str:
    """Reduce free text to a SAM-legal short noun phrase, or ""."""
    cleaned = "".join(char for char in text if char not in CONCEPT_FORBIDDEN)
    return " ".join(cleaned.split()[:10])


def _sam_target_for(
    target: Observation, claim_text: str, vehicle: dict | None = None
) -> tuple[str, str, int | None]:
    """Build the SAM prompt from the observed physical object (Layer C).

    Every control item in the observation (top-level plus validated
    nested controls) is interpreted against the claim; the first
    supporting item wins, then the first claim-relevant (feature
    matching) item, then top-level order — so SAM localizes the control
    the claim is actually about instead of an unrelated object that
    shares the photo. The concept comes from the winning item's
    ``visible_object`` (falling back to ``control_symbol``), never from
    an abstract capability name. Legacy rows without control
    identification fall back to the claim text; when no item yields a
    concept phrase the caller records UNCLEAR without spending.
    Returns (concept, target_source, control_index).
    """
    extra = target.extra or {}
    if "visible_object" not in extra:
        return sanitize_concept(claim_text), "claim_fallback", None
    catalog = get_catalog()
    feature = adas_evidence.normalize_feature(claim_text)
    ranked = []
    for item in adas_evidence.iter_control_items(extra):
        interpretation = adas_evidence.interpret_control(
            catalog,
            feature,
            visible_object=item.visible_object,
            control_symbol=item.control_symbol,
            control_type=item.control_type,
            location=item.location,
            dedicated=item.dedicated,
            manufacturer_context=item.manufacturer_context,
            vehicle=vehicle,
        )
        ranked.append((item, interpretation))
    ranked.sort(
        key=lambda pair: (
            (
                0
                if pair[1].supports_claim
                else (
                    1
                    if pair[1].entry_id is not None and pair[1].feature == feature
                    else 2
                )
            ),
            pair[0].index,
        )
    )
    for item, _ in ranked:
        for candidate in (item.visible_object, item.control_symbol):
            if candidate.strip() and candidate.strip().lower() != "none":
                concept = sanitize_concept(candidate)
                if concept:
                    return concept, "observed_object", item.index
    return "", "unavailable", None


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
    snapshot = get_snapshot(job)
    id_to_text = {
        entry["id"]: entry["text"]
        for entry in snapshot.get("claims", [])
        if entry.get("text")
    }
    snapshot_vehicle = snapshot.get("vehicle")
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
        text = id_to_text.get(target.claim_key)
        if text is None:
            detail = f"Muse observation references unknown claim {target.claim_key!r}"
            services.fail_job(job_id, worker_id, "unknown_claim", detail)
            raise services.JobNotRunningError(f"job {job_id} has unknown claim")
        concept, target_source, control_index = _sam_target_for(
            target, text, snapshot_vehicle
        )
        photo = target.photo
        if not concept:
            with transaction.atomic():
                services.fence_evidence_write(job_id, worker_id)
                Observation.objects.create(
                    job=job,
                    claim_key=target.claim_key,
                    photo=photo,
                    source=ObservationSource.SAM,
                    verdict=Verdict.UNCLEAR,
                    rationale="no reliable SAM target: observed object and "
                    "claim both yield no concept phrase",
                )
            continue
        data = verified_original_bytes(job_id, worker_id, photo)
        request = SamRequest(
            photo=ImageAttachment(
                photo_id=str(photo.id), mime_type=photo.mime_type, data=data
            ),
            image_width=photo.width,
            image_height=photo.height,
            concept=concept,
        )
        result, attempts, cost, settled = _guarded_analyze(
            job_id,
            worker_id,
            own_adapter,
            request,
            stage=Stage.SAM,
            provider=Provider.META,
            model_id=own_adapter.model_id,
            photo_ids=[str(photo.id)],
        )
        usage = result.usage
        execution = services.record_execution(
            job_id,
            worker_id,
            stage=Stage.SAM,
            provider=Provider.META,
            model_id=result.model,
            photo_ids=[str(photo.id)],
            attempts=attempts,
            latency_ms=usage.latency_ms,
            usage={
                "pricing_version": usage.pricing_version,
                "warnings": list(result.warnings),
            },
            cost_usd=cost,
        )
        with transaction.atomic():
            # Fenced before the first mask file lands: a stale worker
            # leaves neither accepted rows nor orphan masks behind.
            services.fence_evidence_write(job_id, worker_id)
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
                extra={
                    "sam_target": concept,
                    "target_source": target_source,
                    "control_index": control_index,
                },
                execution=execution,
            )
        localized += 1
        # Spend is booked; the SAM observation above is kept so a
        # resumed job never re-pays for this localization.
        _raise_if_halted(settled, job_id, Stage.SAM, worker_id)
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
        data = verified_original_bytes(job_id, worker_id, photo)
        with Image.open(io.BytesIO(data)) as img:
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
                with transaction.atomic():
                    services.fence_evidence_write(job_id, worker_id)
                    ref = uuid.uuid4().hex
                    dest = audit_dirs(job_id)["crops"] / f"{ref}.png"
                    tmp = dest.with_suffix(".tmp")
                    img.crop((x1, y1, x2, y2)).save(tmp, format="PNG")
                    tmp.replace(dest)
                    AuditCrop.objects.create(
                        job=job,
                        source_photo=photo,
                        source_observation=obs,
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


RECHECK_PROMPT_VERSION = "m2-recheck"

RECHECK_SYSTEM_INSTRUCTIONS = (
    "You are an ADAS feature spotter for used-car listing audits. "
    "Two images are attached: the full source photo for context, then a "
    "zoomed crop cut from it to inspect one region closely. Judge the "
    "crop with the full photo as context, and report exactly what they "
    "show for the single advertised claim under review: never guess from "
    "the listing text, never label the feature present merely because "
    "the claim suggests it, and flag ambiguity explicitly. Respond with "
    "a JSON object containing an 'observations' array; each item has "
    "photo_id (the source photo), claim_concept (the claim ID, e.g. c1), "
    "visible_object (a short string naming the visible object in the "
    'crop, or the literal string "none"), description, '
    "supports_localization (boolean), ambiguous (boolean), needs_crop "
    "(boolean), needs_other_view (boolean). "
    "Also report control_symbol (exact icon or markings in the crop), "
    "control_type, location (steering_wheel, left_dashboard, stalk, "
    "instrument_cluster, infotainment, center_console, exterior_sensor, "
    "windshield_camera, unknown), and dedicated (true only for a "
    "clearly single-feature control). If the crop is unreadable, say so "
    "instead of guessing."
)


def crop_needs_recheck(crop: AuditCrop, job: AuditJob) -> bool:
    """A crop earns a paid recheck only on documented ambiguity: Muse
    explicitly asked for a closer look (needs_crop), or Muse and SAM
    disagree on the pair. Confirmed sightings are evidence as-is.
    (Crops without a linked source observation are skipped by
    run_recheck before this check; the None branch below is defensive.)"""
    sam_obs = crop.source_observation
    if sam_obs is None:
        return True
    muse_obs = (
        Observation.objects.filter(
            job=job,
            claim_key=sam_obs.claim_key,
            photo_id=crop.source_photo_id,
            source=ObservationSource.Muse,
        )
        .order_by("-created_at")
        .first()
    )
    if muse_obs is None:
        return True
    if muse_obs.extra.get("needs_crop"):
        return True
    return muse_obs.verdict != sam_obs.verdict


def run_recheck(
    job_id,
    worker_id: str,
    adapter: BasetenMuseAdapter | None = None,
) -> int:
    """Re-examine ambiguous crops with Muse: at most one call per crop,
    scoped to the crop's own originating claim, bounded by the recheck
    cap. A crop earns its call only on documented ambiguity (see
    crop_needs_recheck); confirmed sightings are evidence as-is, and
    crops without a linked source observation are skipped (no documented
    relation, no spend). Coverage is crop-aware: distinct regions for the
    same claim/photo each keep their own observation and execution
    history, including disagreements. Returns provider calls made."""
    job = AuditJob.objects.get(pk=job_id)
    services.check_budget(job, worker_id)
    own_adapter = adapter or BasetenMuseAdapter(
        model_id=settings.AUDIT_BASETEN_MODEL_ID
    )
    cap = settings.AUDIT_MAX_RECHECKS_PER_JOB
    id_to_text = {
        entry["id"]: entry["text"]
        for entry in get_snapshot(job).get("claims", [])
        if entry.get("text")
    }
    covered: set[tuple[str, str, str]] = set()
    legacy_covered: set[tuple[str, str]] = set()
    for claim_key, photo_id, crop_id in Observation.objects.filter(
        job=job, source=ObservationSource.RECHECK
    ).values_list("claim_key", "photo_id", "source_crop_id"):
        if crop_id is None:
            legacy_covered.add((claim_key, str(photo_id)))
        else:
            covered.add((claim_key, str(photo_id), str(crop_id)))
    calls = 0
    skipped_confirmed = 0
    skipped_unlinked = 0
    crops_dir = audit_dirs(job_id)["crops"]
    crops = (
        AuditCrop.objects.filter(job=job)
        .select_related("source_observation", "source_photo")
        .order_by("created_at")
    )
    for crop in crops:
        photo = crop.source_photo
        sam_obs = crop.source_observation
        if sam_obs is None:
            skipped_unlinked += 1
            continue
        claim_id = sam_obs.claim_key
        if not crop_needs_recheck(crop, job):
            skipped_confirmed += 1
            continue
        key = (claim_id, str(photo.pk), str(crop.pk))
        if key in covered or (claim_id, str(photo.pk)) in legacy_covered:
            continue
        if calls >= cap:
            break
        expected = {(claim_id, str(photo.pk))}
        if not services.heartbeat(job_id, worker_id):
            raise services.JobNotRunningError(f"job {job_id} stopped during recheck")
        data = (crops_dir / f"{crop.storage_ref}.png").read_bytes()
        context = verified_original_bytes(job_id, worker_id, photo)
        claim_text = id_to_text.get(claim_id, claim_id)
        request = MuseRequest(
            prompt_version=RECHECK_PROMPT_VERSION,
            system_instructions=RECHECK_SYSTEM_INSTRUCTIONS,
            user_text=(
                build_user_text([{"id": claim_id, "text": claim_text}], [str(photo.id)])
                + "\n\nTwo images follow: the full photo for context, then "
                f"a zoomed crop from photo {photo.id} targeting claim "
                f"{claim_id} ({sam_obs.rationale}). Judge only whether THIS "
                "crop region clarifies that one claim, with the full photo "
                "as context."
            ),
            photo_ids=(str(photo.id),),
            images=(
                ImageAttachment(
                    photo_id=str(photo.id),
                    mime_type=photo.mime_type,
                    data=context,
                ),
                ImageAttachment(
                    photo_id=str(photo.id), mime_type="image/png", data=data
                ),
            ),
            max_output_tokens=MUSE_MAX_OUTPUT_TOKENS,
        )
        result, attempts, cost, settled = _guarded_analyze(
            job_id,
            worker_id,
            own_adapter,
            request,
            stage=Stage.RECHECK,
            provider=Provider.BASETEN,
            model_id=own_adapter.model_id,
            photo_ids=[str(photo.id)],
        )
        calls += 1
        usage = result.usage
        seen, warnings, fatal = validate_batch(result.observations, expected, set())
        execution = services.record_execution(
            job_id,
            worker_id,
            stage=Stage.RECHECK,
            provider=Provider.BASETEN,
            model_id=result.model,
            photo_ids=[str(photo.id)],
            attempts=attempts,
            latency_ms=usage.latency_ms,
            usage={
                "prompt_version": usage.prompt_version,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "pricing_version": usage.pricing_version,
                "crop_ref": crop.storage_ref,
                "crop_id": str(crop.pk),
                "validation_warnings": list(result.validation_warnings) + warnings,
            },
            cost_usd=cost,
        )
        if fatal is not None:
            code, detail = fatal
            services.fail_job(job_id, worker_id, code, detail)
            raise ProviderResponseError(detail)
        prior = (
            Observation.objects.filter(
                job=job,
                claim_key=claim_id,
                photo=photo,
                source=ObservationSource.Muse,
            )
            .order_by("-created_at")
            .values_list("verdict", flat=True)
            .first()
        )
        with transaction.atomic():
            services.fence_evidence_write(job_id, worker_id)
            for (cid, _), item in seen.items():
                verdict = _verdict(item)
                _, created = Observation.objects.get_or_create(
                    job=job,
                    claim_key=cid,
                    photo=photo,
                    source=ObservationSource.RECHECK,
                    source_crop=crop,
                    defaults={
                        "verdict": verdict,
                        "rationale": item.get("description", ""),
                        "extra": {
                            "crop_ref": crop.storage_ref,
                            "changed": prior is not None and prior != verdict,
                            **_control_extra(item),
                        },
                        "execution": execution,
                    },
                )
                if created:
                    covered.add(key)
        # Spend is booked; the recheck observation above is kept
        # so a resumed job never re-pays for this crop.
        _raise_if_halted(settled, job_id, Stage.RECHECK, worker_id)
    services.record_execution(
        job_id,
        worker_id,
        stage=Stage.RECHECK,
        provider=Provider.LOCAL,
        usage={
            "calls": calls,
            "skipped_confirmed": skipped_confirmed,
            "skipped_unlinked": skipped_unlinked,
            "cap": cap,
        },
    )
    return calls


# Claims matching these patterns intrinsically need nonvisual confirmation
# (software state, history, coverage): no photograph can establish them, so
# unresolved cases route to NOT_VISUALLY_VERIFIABLE instead of generic
# INSUFFICIENT. Deliberately narrow v1 heuristic: photo evidence that does
# show the claim still wins (checked first), and a wrong label here only
# changes human-review guidance, never a verification verdict.
INTRINSICALLY_NONVISUAL = re.compile(
    r"software|firmware|version|update|recall|calibrat|service history|"
    r"service records|warranty|subscription|activation|odometer|title status|"
    r"accident history",
    re.IGNORECASE,
)


def _keyed_present(
    observations: list[Observation],
) -> list[Observation]:
    """Muse-family PRESENT rows carrying Layer B control identification."""
    return [
        obs
        for obs in observations
        if obs.source in _MUSE_FAMILY
        and obs.verdict == Verdict.PRESENT
        and "visible_object" in (obs.extra or {})
    ]


def reconcile_claim(
    observations: list[Observation],
    claim_text: str = "",
    vehicle: dict | None = None,
) -> tuple[str, str]:
    """Deterministic five-state rule for one claim (pure, testable).

    Conservative by design (Rule E): missing visible evidence is never
    negative, so photo-only audits can only reach VISUALLY_SUPPORTED,
    INSUFFICIENT, or NOT_VISUALLY_VERIFIABLE. CONTRADICTED and VERIFIED
    require authoritative vehicle-specific evidence (the channel exists
    for hand-supplied docs evidence).
    Layer D (rule v4): every control item in PRESENT observations
    carrying control identification is evaluated independently through
    the manufacturer-aware catalog; a diagnostic/supportive mapping
    supports the claim even without SAM confirmation, while unmapped or
    non-supportive controls explain exactly why they fall short.
    Manufacturer-specific entries apply only when ``vehicle`` (the
    snapshot's trusted listing metadata) falls inside their documented
    make/model/year applicability; unknown identity rejects them. Rows
    without control identification keep the legacy v2 confirm rules.
    Returns (state, summary).
    """
    authoritative = [
        obs for obs in observations if obs.source == ObservationSource.AUTHORITATIVE
    ]
    if authoritative:
        verdicts = {obs.verdict for obs in authoritative}
        if verdicts == {Verdict.PRESENT}:
            return (
                EvidenceState.VERIFIED,
                "authoritative vehicle-specific evidence establishes the feature",
            )
        if verdicts == {Verdict.ABSENT}:
            return (
                EvidenceState.CONTRADICTED,
                "authoritative vehicle-specific evidence conflicts with the claim",
            )
        return (
            EvidenceState.INSUFFICIENT,
            "authoritative evidence disagrees with itself; human review needed",
        )
    catalog = get_catalog()
    feature = adas_evidence.normalize_feature(claim_text)
    keyed = _keyed_present(observations)
    v3_unknown_note = ""
    if keyed:
        candidates = []
        for obs in keyed:
            for item in adas_evidence.iter_control_items(obs.extra or {}):
                candidates.append(
                    (
                        adas_evidence.interpret_control(
                            catalog,
                            feature,
                            visible_object=item.visible_object,
                            control_symbol=item.control_symbol,
                            control_type=item.control_type,
                            location=item.location,
                            dedicated=item.dedicated,
                            manufacturer_context=item.manufacturer_context,
                            vehicle=vehicle,
                        ),
                        obs,
                        item.index,
                    )
                )
        supporters = [c for c in candidates if c[0].supports_claim]
        if supporters:
            best, best_obs, best_index = max(
                supporters,
                key=lambda c: adas_evidence.STRENGTH_RANK[c[0].strength],
            )
            sam_present = any(
                obs.source == ObservationSource.SAM and obs.verdict == Verdict.PRESENT
                for obs in observations
            )
            sam_note = (
                "confirmed by SAM localization"
                if sam_present
                else "SAM localization unavailable; mapping stands on the "
                "identified control alone"
            )
            photo_ref = (
                str(best_obs.photo_id)[:8] if best_obs.photo_id is not None else "?"
            )
            return (
                EvidenceState.VISUALLY_SUPPORTED,
                (
                    f"{best.explanation} [{best.strength}; {sam_note}; "
                    f"catalog {catalog.version}/{best.entry_id}; "
                    f"control #{best_index} (photo {photo_ref}); photo evidence "
                    "only, not capability or condition proof]"
                ),
            )
        strongest, _, _ = max(
            candidates,
            key=lambda c: adas_evidence.STRENGTH_RANK.get(c[0].strength, -1),
        )
        v3_unknown_note = (
            f"{strongest.explanation}; "
            f"catalog {catalog.version} offers no support for {feature or 'this claim'}"
        )
    legacy = [
        obs
        for obs in observations
        if not (obs.source in _MUSE_FAMILY and "visible_object" in (obs.extra or {}))
    ]
    present_sources = {obs.source for obs in legacy if obs.verdict == Verdict.PRESENT}
    if ObservationSource.Muse in present_sources and (
        ObservationSource.SAM in present_sources
    ):
        return (
            EvidenceState.VISUALLY_SUPPORTED,
            (
                "Muse sighting confirmed by SAM localization "
                "(photo evidence only; not capability proof)"
            ),
        )
    if ObservationSource.RECHECK in present_sources:
        return (
            EvidenceState.VISUALLY_SUPPORTED,
            (
                "crop recheck confirmed the feature "
                "(photo evidence only; not capability proof)"
            ),
        )
    if claim_text and INTRINSICALLY_NONVISUAL.search(claim_text):
        return (
            EvidenceState.NOT_VISUALLY_VERIFIABLE,
            "claim needs nonvisual confirmation; photos cannot resolve it",
        )
    if v3_unknown_note:
        return (EvidenceState.INSUFFICIENT, v3_unknown_note)
    n_muse = sum(1 for obs in observations if obs.source == ObservationSource.Muse)
    if n_muse:
        return (
            EvidenceState.INSUFFICIENT,
            (
                f"no confirmed sighting in {n_muse} photo observation(s); "
                "absence from photos is not evidence of absence"
            ),
        )
    return (
        EvidenceState.INSUFFICIENT,
        "no observations for this claim",
    )


def run_reconcile(job_id, worker_id: str) -> int:
    """Apply the deterministic rules per claim, persist findings, complete
    the job. Reruns overwrite findings (update_or_create), never duplicate."""
    job = AuditJob.objects.get(pk=job_id)
    services.check_budget(job, worker_id)
    snapshot = get_snapshot(job)
    entries = [e for e in snapshot.get("claims", []) if e.get("text")]
    count = 0
    for entry in entries:
        claim, text = entry["id"], entry["text"]
        observations = list(
            Observation.objects.filter(job=job, claim_key=claim).order_by("created_at")
        )
        vehicle = snapshot.get("vehicle")
        state, summary = reconcile_claim(observations, text, vehicle)
        recheck_ran = any(o.source == ObservationSource.RECHECK for o in observations)
        state_without, _ = reconcile_claim(
            [o for o in observations if o.source != ObservationSource.RECHECK],
            text,
            vehicle,
        )
        interpretation = adas_evidence.explain_claim(
            get_catalog(), observations, text, vehicle
        )
        with transaction.atomic():
            services.fence_evidence_write(job_id, worker_id)
            AuditFinding.objects.update_or_create(
                job=job,
                claim_key=claim,
                defaults={
                    "claim_text": text,
                    "state": state,
                    "summary": summary,
                    "evidence": {
                        "rule": "v4",
                        "recheck_ran": recheck_ran,
                        "recheck_changed_finding": recheck_ran
                        and state_without != state,
                        "interpretation": interpretation,
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
