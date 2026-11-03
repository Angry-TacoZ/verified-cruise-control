"""Audit engine: queue reuse, atomic claim, heartbeat, cancel, budget, stages."""

from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connections
from django.utils import timezone

from audits import pipeline, services
from audits.models import (
    AuditExecution,
    AuditJob,
    JobStatus,
    Observation,
    ObservationSource,
    Provider,
    Stage,
    Verdict,
)
from evidence import images
from listings.models import Listing
from providers.base import (
    ProviderRateLimitError,
    ProviderResponseError,
    UsageRecord,
)
from providers.baseten import MuseObservationResult

from .conftest import make_image_bytes

pytestmark = pytest.mark.django_db


@pytest.fixture()
def enabled(settings):
    settings.AUDIT_INFERENCE_ENABLED = True
    return settings


@pytest.fixture()
def listing():
    return Listing.objects.create(
        source_url="https://example.test/l/audit-1",
        source_site="example.test",
        title="audit listing",
        advertised_claims=["adaptive cruise control", "lane keeping assist"],
        expected_photo_count=2,
    )


@pytest.fixture()
def photos(listing):
    out = []
    for label in ("A1", "A2"):
        photo, _ = images.store_listing_photo(
            listing,
            data=make_image_bytes("PNG", label),
            filename=f"{label}.png",
            source_url=None,
            provenance="extension",
        )
        out.append(photo)
    return out


def _running_job(listing, worker="w1", **overrides):
    now = timezone.now()
    params = {
        "listing": listing,
        "status": JobStatus.RUNNING,
        "worker_id": worker,
        "leased_at": now,
        "heartbeat_at": now,
    }
    params.update(overrides)
    return AuditJob.objects.create(**params)


# --- queue + reuse -----------------------------------------------------


def test_queue_refuses_when_disabled(listing, settings):
    settings.AUDIT_INFERENCE_ENABLED = False
    with pytest.raises(services.InferenceDisabledError):
        services.queue_audit(listing.pk)


def test_queue_reuses_in_flight_job(listing, enabled):
    job, created = services.queue_audit(listing.pk, requested_by="op")
    assert created is True
    assert job.max_calls == enabled.AUDIT_MAX_CALLS_PER_JOB
    same, created_again = services.queue_audit(listing.pk)
    assert created_again is False
    assert same.pk == job.pk
    assert AuditJob.objects.filter(listing=listing).count() == 1


def test_queue_new_job_after_terminal(listing, enabled):
    job, _ = services.queue_audit(listing.pk)
    job.status = JobStatus.COMPLETE
    job.save()
    job2, created = services.queue_audit(listing.pk)
    assert created is True
    assert job2.pk != job.pk


@pytest.mark.django_db(transaction=True)
def test_concurrent_queue_single_job(listing, enabled):
    """Simultaneous submissions converge on one active audit: losers get
    the winner instead of creating duplicate paid work."""
    import threading

    barrier = threading.Barrier(8)
    results: list = []
    errors: list[Exception] = []

    def _queue() -> None:
        from django.db import connections

        try:
            barrier.wait(timeout=10)
            results.append(services.queue_audit(listing.pk))
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=_queue) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 8
    ids = {str(job.pk) for job, _ in results}
    assert ids == {str(AuditJob.objects.get().pk)}
    assert sum(1 for _, created in results if created) == 1
    listing.delete()


# --- claim -------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_concurrent_claim_single_winner(listing, enabled):
    job, _ = services.queue_audit(listing.pk)
    winners: list = []
    errors: list[Exception] = []

    def _claim(worker: str) -> None:
        try:
            claimed = services.claim_job(worker)
            if claimed is not None:
                winners.append(worker)
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=_claim, args=(f"w{i}",)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(winners) == 1
    job.refresh_from_db()
    assert job.status == JobStatus.RUNNING
    assert job.worker_id == winners[0]
    listing.delete()


def test_claim_reclaims_stale_job(listing):
    stale = _running_job(listing, heartbeat_at=timezone.now() - timedelta(seconds=3600))
    claimed = services.claim_job("w2")
    assert claimed is not None
    assert claimed.pk == stale.pk
    assert claimed.worker_id == "w2"


def test_claim_ignores_fresh_running_job(listing):
    _running_job(listing)
    assert services.claim_job("w2") is None


def test_claim_empty_queue_returns_none():
    assert services.claim_job("w1") is None


# --- heartbeat + cancel --------------------------------------------------


def test_heartbeat_ok_while_running(listing):
    job = _running_job(listing)
    assert services.heartbeat(job.pk, "w1") is True


def test_heartbeat_false_for_stale_worker(listing):
    job = _running_job(listing)
    assert services.heartbeat(job.pk, "intruder") is False


def test_cancel_stops_running_worker(listing):
    job = _running_job(listing)
    assert services.cancel_job(job.pk) is True
    assert services.heartbeat(job.pk, "w1") is False
    job.refresh_from_db()
    assert job.status == JobStatus.CANCELLED


def test_cancel_terminal_job_is_noop(listing):
    job = _running_job(listing)
    services.complete_job(job.pk, "w1")
    assert services.cancel_job(job.pk) is False


# --- budget --------------------------------------------------------------


def test_record_local_stage_not_charged(listing):
    job = _running_job(listing)
    services.record_execution(
        job.pk,
        "w1",
        stage=Stage.SCREENING,
        provider=Provider.LOCAL,
        attempts=3,
    )
    job.refresh_from_db()
    assert job.model_calls_made == 0
    assert job.spend_usd == Decimal("0.0000")
    assert job.unknown_cost_calls == 0
    assert job.status == JobStatus.RUNNING


def test_reserve_then_settle_known_cost(listing):
    job = _running_job(listing)
    services.reserve_call(job.pk, "w1", max_attempts=3, reserve_usd=Decimal("0.15"))
    job.refresh_from_db()
    assert job.reserved_calls == 3
    assert job.reserved_spend_usd == Decimal("0.15")
    assert job.model_calls_made == 0
    settled = services.settle_call(job.pk, "w1", attempts=1, cost_usd=Decimal("0.0201"))
    assert settled.model_calls_made == 1
    assert settled.spend_usd == Decimal("0.0201")
    assert settled.reserved_calls == 0
    assert settled.reserved_spend_usd == Decimal("0.0000")
    assert settled.status == JobStatus.RUNNING


def test_unknown_cost_pauses_job(listing):
    job = _running_job(listing)
    services.reserve_call(job.pk, "w1", max_attempts=3, reserve_usd=Decimal("0.15"))
    settled = services.settle_call(job.pk, "w1", attempts=1, cost_usd=None)
    assert settled.status == JobStatus.PAUSED
    assert settled.error_code == "unknown_cost"
    assert settled.unknown_cost_calls == 1
    assert settled.reserved_calls == 0
    # Spend never absorbs the unknown call as zero.
    assert settled.spend_usd == Decimal("0.0000")


def test_reserve_over_spend_cap_fails_closed(listing):
    job = _running_job(listing, spend_cap_usd=Decimal("0.05"))
    with pytest.raises(services.BudgetExceededError) as excinfo:
        services.reserve_call(job.pk, "w1", max_attempts=1, reserve_usd=Decimal("0.15"))
    assert excinfo.value.code == "spend_cap"
    job.refresh_from_db()
    assert job.status == JobStatus.PAUSED
    assert job.error_code == "spend_cap"
    assert job.model_calls_made == 0
    assert job.reserved_calls == 0


def test_reserve_retry_headroom_consumes_call_cap(listing):
    job = _running_job(listing, max_calls=2)
    with pytest.raises(services.BudgetExceededError) as excinfo:
        services.reserve_call(job.pk, "w1", max_attempts=3, reserve_usd=Decimal("0.01"))
    assert excinfo.value.code == "call_cap"
    job.refresh_from_db()
    assert job.status == JobStatus.PAUSED
    assert job.error_code == "call_cap"
    assert job.model_calls_made == 0


def test_settle_retry_within_allowance(listing):
    job = _running_job(listing, max_calls=4)
    services.reserve_call(job.pk, "w1", max_attempts=3, reserve_usd=Decimal("0.15"))
    settled = services.settle_call(job.pk, "w1", attempts=3, cost_usd=Decimal("0.0400"))
    assert settled.model_calls_made == 3
    assert settled.spend_usd == Decimal("0.0400")
    assert settled.status == JobStatus.RUNNING
    # Headroom for a second logical call no longer fits.
    with pytest.raises(services.BudgetExceededError) as excinfo:
        services.reserve_call(job.pk, "w1", max_attempts=3, reserve_usd=Decimal("0.01"))
    assert excinfo.value.code == "call_cap"


def test_stale_worker_cannot_write(listing):
    job = _running_job(listing)
    # Provenance rows are append-only: a stale worker's row lands, but the
    # job ledger and lifecycle are untouched (results stay first-wins).
    services.record_execution(
        job.pk, "intruder", stage=Stage.SCREENING, provider=Provider.LOCAL
    )
    job.refresh_from_db()
    assert job.status == JobStatus.RUNNING
    assert job.model_calls_made == 0
    assert job.spend_usd == Decimal("0.0000")
    with pytest.raises(services.StaleWorkerError):
        services.complete_job(job.pk, "intruder")


def test_paused_job_rejects_writes(listing):
    job = _running_job(listing, status=JobStatus.PAUSED)
    # Provenance rows still land (settle pauses, then the stage records),
    # but reservations and lifecycle transitions are rejected.
    services.record_execution(
        job.pk, "w1", stage=Stage.SCREENING, provider=Provider.LOCAL
    )
    with pytest.raises(services.JobNotRunningError):
        services.reserve_call(job.pk, "w1", max_attempts=1, reserve_usd=Decimal("0.01"))
    with pytest.raises(services.StaleWorkerError):
        services.complete_job(job.pk, "w1")


# --- screening -----------------------------------------------------------


def test_screening_happy_path(listing, photos):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    assert screened.claims == ["adaptive cruise control", "lane keeping assist"]
    assert len(screened.photos) == 2
    row = AuditExecution.objects.get(job=job)
    assert row.stage == Stage.SCREENING
    assert row.provider == Provider.LOCAL
    assert row.cost_usd is None


def test_screening_no_photos_fails(listing):
    job = _running_job(listing)
    with pytest.raises(services.JobNotRunningError):
        pipeline.run_screening(job.pk, "w1")
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_code == "no_photos"


def test_screening_no_claims_fails(listing, photos):
    listing.advertised_claims = []
    listing.save()
    job = _running_job(listing)
    with pytest.raises(services.JobNotRunningError):
        pipeline.run_screening(job.pk, "w1")
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_code == "no_claims"


# --- Muse stage (fake adapter, zero inference) ---------------------------


class FakeMuseAdapter:
    model_id = "fake-muse"
    last_attempts = 1

    def __init__(self, observations=None, error=None, cost=0.0123):
        self._observations = observations or []
        self._error = error
        self._cost = cost
        self.calls = 0

    def analyze(self, request):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return MuseObservationResult(
            observations=tuple(self._observations),
            model=self.model_id,
            prompt_version=request.prompt_version,
            usage=UsageRecord(
                provider="baseten",
                model_id=self.model_id,
                prompt_version=request.prompt_version,
                photo_ids=tuple(request.photo_ids),
                status="ok",
                latency_ms=11,
                input_tokens=100,
                output_tokens=50,
                estimated_cost_usd=self._cost,
                pricing_version="test",
            ),
            validation_warnings=(),
        )


def _obs(photo_id, concept, **overrides):
    item = {
        "photo_id": photo_id,
        "claim_concept": concept,
        "visible_object": "button cluster",
        "description": "button cluster visible",
        "supports_localization": True,
        "ambiguous": False,
        "needs_crop": False,
        "needs_other_view": False,
    }
    item.update(overrides)
    return item


def test_muse_persists_verdicts(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter(
        [
            [
                _obs(str(photos[0].id), "c1"),
                _obs(str(photos[0].id), "c2", ambiguous=True),
            ],
            [
                _obs(str(photos[1].id), "c1", visible_object="none"),
                _obs(str(photos[1].id), "c2", visible_object="none"),
            ],
        ]
    )
    stored = pipeline.run_muse(job.pk, "w1", screened, adapter)
    assert stored == 4
    rows = {
        (o.claim_key, str(o.photo_id)): o.verdict
        for o in Observation.objects.filter(job=job)
    }
    assert rows[("c1", str(photos[0].id))] == Verdict.PRESENT
    assert rows[("c1", str(photos[1].id))] == Verdict.ABSENT
    assert rows[("c2", str(photos[0].id))] == Verdict.UNCLEAR
    job.refresh_from_db()
    assert job.spend_usd == Decimal("0.0123") * 2


def test_muse_rerun_is_idempotent(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter(
        [
            [
                _obs(str(photos[0].id), "c1"),
                _obs(str(photos[0].id), "c2", visible_object="none"),
            ],
            [
                _obs(str(photos[1].id), "c1", visible_object="none"),
                _obs(str(photos[1].id), "c2", visible_object="none"),
            ],
        ]
    )
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 4
    assert adapter.calls == 2
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 0
    assert adapter.calls == 2
    assert Observation.objects.filter(job=job).count() == 4


def test_muse_provider_error_fails_job(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = FakeMuseAdapter(error=ProviderRateLimitError("slow down"))
    with pytest.raises(ProviderRateLimitError):
        pipeline.run_muse(job.pk, "w1", screened, adapter)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_code == "ProviderRateLimitError"
    row = AuditExecution.objects.get(job=job, stage=Stage.Muse)
    assert row.ok is False


def test_muse_unknown_cost_pauses(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter(
        [
            [
                _obs(str(photos[0].id), "c1"),
                _obs(str(photos[0].id), "c2", visible_object="none"),
            ]
        ],
        cost=None,
    )
    with pytest.raises(services.BudgetExceededError):
        pipeline.run_muse(job.pk, "w1", screened, adapter)
    job.refresh_from_db()
    assert job.status == JobStatus.PAUSED
    assert job.error_code == "unknown_cost"
    assert job.unknown_cost_calls == 1
    assert job.spend_usd == Decimal("0.0000")
    assert job.reserved_calls == 0
    # The validated observations are kept: the call may already have been
    # billed server-side, and a resumed audit must never re-pay for them.
    assert Observation.objects.filter(job=job).count() == 2
    row = AuditExecution.objects.filter(
        job=job, stage=Stage.Muse, provider=Provider.BASETEN
    ).get()
    assert row.cost_usd is None
    assert row.attempts == 1


def test_run_stages_rejects_stale_worker(listing, photos, enabled):
    job = _running_job(listing)
    with pytest.raises(services.StaleWorkerError):
        pipeline.run_stages(job.pk, "intruder")


def test_current_stage_defaults(listing):
    queued = AuditJob.objects.create(listing=listing)
    assert queued.current_stage == JobStatus.QUEUED
    queued.status = JobStatus.COMPLETE
    queued.save()
    running = _running_job(listing)
    assert running.current_stage == Stage.SCREENING


def test_complete_job(listing):
    job = _running_job(listing)
    services.complete_job(job.pk, "w1")
    job.refresh_from_db()
    assert job.status == JobStatus.COMPLETE


# --- SAM stage (fake adapter, zero inference) ----------------------------


class FakeSamAdapter:
    model_id = "fake-sam"
    last_attempts = 1

    def __init__(self, records=None, error=None, cost=0.0025):
        self._records = records or []
        self._error = error
        self._cost = cost
        self.calls = 0
        self.concepts: list[str] = []

    def analyze(self, request):
        from providers.meta import SamSegmentationResult

        self.calls += 1
        self.concepts.append(request.concept)
        if self._error is not None:
            raise self._error
        return SamSegmentationResult(
            records=tuple(self._records),
            model=self.model_id,
            usage=UsageRecord(
                provider="meta",
                model_id=self.model_id,
                prompt_version="sam-test",
                photo_ids=(request.photo.photo_id,),
                status="ok",
                latency_ms=9,
                estimated_cost_usd=self._cost,
                pricing_version="test",
            ),
            warnings=(),
        )


def _segment(box=(8, 8, 32, 32), size=(64, 48), confidence=0.9):
    from providers.meta import SegmentRecord

    width, height = size
    return SegmentRecord(
        object_id=0,
        box=box,
        width=width,
        height=height,
        raster=bytes(width * height),
        encoding="one_bit",
        provider_confidence=confidence,
    )


def _present_muse_obs(job, photo, claim="c1", **extra):
    flags = {"supports_localization": True}
    flags.update(extra)
    return Observation.objects.create(
        job=job,
        claim_key=claim,
        photo=photo,
        source=ObservationSource.Muse,
        verdict=Verdict.PRESENT,
        rationale="saw it",
        extra=flags,
    )


def test_sanitize_concept():
    assert pipeline.sanitize_concept("adaptive cruise control") == (
        "adaptive cruise control"
    )
    assert pipeline.sanitize_concept("a! b? c; d. e<f> g|h") == "a b c d ef gh"
    long = " ".join(f"w{i}" for i in range(20))
    assert pipeline.sanitize_concept(long) == " ".join(f"w{i}" for i in range(10))
    assert pipeline.sanitize_concept("!?.") == ""


def test_sam_localizes_flagged_present(listing, photos, enabled):
    job = _running_job(listing)
    _present_muse_obs(job, photos[0])
    adapter = FakeSamAdapter(records=[_segment()])
    assert pipeline.run_sam(job.pk, "w1", adapter) == 1
    assert adapter.calls == 1
    assert adapter.concepts == ["adaptive cruise control"]
    obs = Observation.objects.get(job=job, source=ObservationSource.SAM)
    assert obs.verdict == Verdict.PRESENT
    assert obs.boxes[0]["box"] == [8, 8, 32, 32]
    assert obs.boxes[0]["confidence"] == 0.9
    mask_file = pipeline.audit_dirs(job.pk)["masks"] / f"{obs.boxes[0]['mask_ref']}.png"
    assert mask_file.exists()
    from PIL import Image

    with Image.open(mask_file) as img:
        assert img.size == (64, 48)
    row = AuditExecution.objects.get(job=job, stage=Stage.SAM)
    assert row.provider == Provider.META
    job.refresh_from_db()
    assert job.spend_usd == Decimal("0.0025")


def test_sam_zero_match_is_absent(listing, photos, enabled):
    job = _running_job(listing)
    _present_muse_obs(job, photos[0])
    adapter = FakeSamAdapter(records=[])
    assert pipeline.run_sam(job.pk, "w1", adapter) == 1
    obs = Observation.objects.get(job=job, source=ObservationSource.SAM)
    assert obs.verdict == Verdict.ABSENT
    assert obs.boxes == []


def test_sam_skips_unflagged_observations(listing, photos, enabled):
    job = _running_job(listing)
    _present_muse_obs(job, photos[0], supports_localization=False)
    adapter = FakeSamAdapter(records=[_segment()])
    assert pipeline.run_sam(job.pk, "w1", adapter) == 0
    assert adapter.calls == 0
    assert not Observation.objects.filter(
        job=job, source=ObservationSource.SAM
    ).exists()


def test_sam_unlocalizable_claim_is_unclear(photos, enabled):
    listing = Listing.objects.create(
        source_url="https://example.test/l/punct",
        source_site="example.test",
        title="punctuation claim",
        advertised_claims=["!?."],
    )
    photo = photos[0]
    photo.listing = listing
    photo.save()
    job = _running_job(listing)
    _present_muse_obs(job, photo)
    adapter = FakeSamAdapter(records=[_segment()])
    assert pipeline.run_sam(job.pk, "w1", adapter) == 0
    assert adapter.calls == 0
    obs = Observation.objects.get(job=job, source=ObservationSource.SAM)
    assert obs.verdict == Verdict.UNCLEAR


def test_sam_unknown_claim_id_fails(listing, photos, enabled):
    job = _running_job(listing)
    _present_muse_obs(job, photos[0], claim="c99")
    adapter = FakeSamAdapter(records=[_segment()])
    with pytest.raises(services.JobNotRunningError):
        pipeline.run_sam(job.pk, "w1", adapter)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_code == "unknown_claim"


def test_sam_resume_skips_localized_pairs(listing, photos, enabled):
    job = _running_job(listing)
    _present_muse_obs(job, photos[0])
    adapter = FakeSamAdapter(records=[_segment()])
    assert pipeline.run_sam(job.pk, "w1", adapter) == 1
    assert pipeline.run_sam(job.pk, "w1", adapter) == 0
    assert adapter.calls == 1


def test_sam_provider_error_fails_job(listing, photos, enabled):
    job = _running_job(listing)
    _present_muse_obs(job, photos[0])
    adapter = FakeSamAdapter(error=ProviderRateLimitError("slow down"))
    with pytest.raises(ProviderRateLimitError):
        pipeline.run_sam(job.pk, "w1", adapter)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_code == "ProviderRateLimitError"


# --- crop stage ----------------------------------------------------------


def _sam_present_obs(job, photo, boxes, claim="adaptive cruise control"):
    return Observation.objects.create(
        job=job,
        claim_key=claim,
        photo=photo,
        source=ObservationSource.SAM,
        verdict=Verdict.PRESENT,
        boxes=boxes,
        rationale="segments",
    )


def test_crop_cuts_boxes_to_files(listing, photos, enabled):
    from audits.models import AuditCrop

    job = _running_job(listing)
    _sam_present_obs(
        job,
        photos[0],
        [{"box": [8, 8, 32, 32], "mask_ref": None, "confidence": 0.9}],
    )
    assert pipeline.run_crop(job.pk, "w1") == 1
    crop = AuditCrop.objects.get(job=job)
    assert (crop.width, crop.height) == (24, 24)
    assert crop.source_photo_id == photos[0].pk
    crop_file = pipeline.audit_dirs(job.pk)["crops"] / f"{crop.storage_ref}.png"
    assert crop_file.exists()
    from PIL import Image

    with Image.open(crop_file) as img:
        assert img.size == (24, 24)
    # Resume is idempotent.
    assert pipeline.run_crop(job.pk, "w1") == 0
    assert AuditCrop.objects.filter(job=job).count() == 1


def test_crop_respects_cap_and_skips_degenerate(listing, photos, enabled):
    from audits.models import AuditCrop

    enabled.AUDIT_MAX_CROPS_PER_JOB = 1
    job = _running_job(listing)
    _sam_present_obs(
        job,
        photos[0],
        [
            {"box": [0, 0, 16, 16], "mask_ref": None},
            {"box": [16, 16, 32, 32], "mask_ref": None},
            {"box": [5, 5, 5, 5], "mask_ref": None},
        ],
    )
    assert pipeline.run_crop(job.pk, "w1") == 1
    assert AuditCrop.objects.filter(job=job).count() == 1
    row = AuditExecution.objects.filter(job=job, stage=Stage.CROP).latest("created_at")
    assert row.provider == Provider.LOCAL
    assert row.usage["skipped"] == 2


# --- recheck stage -------------------------------------------------------


def _crop_row(
    job, photo, claim="c1", sam_verdict=Verdict.PRESENT, link=True, sam_obs=None
):
    import uuid

    from audits.models import AuditCrop

    ref = uuid.uuid4().hex
    dest = pipeline.audit_dirs(job.pk)["crops"] / f"{ref}.png"
    from PIL import Image

    Image.new("RGB", (16, 16)).save(dest, format="PNG")
    if link and sam_obs is None:
        sam_obs = Observation.objects.create(
            job=job,
            claim_key=claim,
            photo=photo,
            source=ObservationSource.SAM,
            verdict=sam_verdict,
            boxes=[{"box": [0, 0, 16, 16], "mask_ref": None}],
            rationale=f"1 segment(s) for concept {claim!r}",
        )
    elif not link:
        sam_obs = None
    return AuditCrop.objects.create(
        job=job,
        source_photo=photo,
        source_observation=sam_obs,
        box=[0, 0, 16, 16],
        storage_ref=ref,
        width=16,
        height=16,
    )


def test_recheck_confirms_crop(listing, photos, enabled):
    job = _running_job(listing)
    crop = _crop_row(job, photos[0])
    adapter = FakeMuseAdapter(observations=[_obs(str(photos[0].id), "c1")])
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 1
    rows = {
        o.claim_key: o.verdict
        for o in Observation.objects.filter(job=job, source=ObservationSource.RECHECK)
    }
    assert rows == {"c1": Verdict.PRESENT}
    obs = Observation.objects.get(job=job, source=ObservationSource.RECHECK)
    assert obs.source_crop_id == crop.pk
    row = AuditExecution.objects.get(
        job=job, stage=Stage.RECHECK, provider=Provider.BASETEN
    )
    assert row.usage["prompt_version"] == "m2-recheck"
    assert row.usage["crop_id"] == str(crop.pk)


def test_recheck_skips_covered_crops(listing, photos, enabled):
    job = _running_job(listing)
    crop = _crop_row(job, photos[0])
    Observation.objects.create(
        job=job,
        claim_key="c1",
        photo=photos[0],
        source=ObservationSource.RECHECK,
        source_crop=crop,
        verdict=Verdict.PRESENT,
        extra={"crop_ref": crop.storage_ref},
    )
    adapter = FakeMuseAdapter(observations=[])
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 0
    assert adapter.calls == 0


def test_recheck_provider_error_fails_job(listing, photos, enabled):
    job = _running_job(listing)
    _crop_row(job, photos[0])
    adapter = FakeMuseAdapter(error=ProviderRateLimitError("slow down"))
    with pytest.raises(ProviderRateLimitError):
        pipeline.run_recheck(job.pk, "w1", adapter)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED


# --- reconcile -----------------------------------------------------------


def _obs_row(job, photo, source, verdict, claim="adaptive cruise control", **extra):
    return Observation.objects.create(
        job=job,
        claim_key=claim,
        photo=photo,
        source=source,
        verdict=verdict,
        extra=extra,
    )


# NOTE: absence-in-photos previously asserted CONTRADICTED here. That was
# the Finding 2 defect (Rule E: missing evidence is not negative). Coverage
# moved to test_conservative_no_radar_in_exteriors (insufficient) and
# test_conservative_authoritative_discrepancy (contradicted only via
# authoritative evidence).


def test_reconcile_supported_via_sam(listing, photos):
    job = _running_job(listing)
    rows = [
        _obs_row(job, photos[0], ObservationSource.Muse, Verdict.PRESENT),
        _obs_row(job, photos[0], ObservationSource.SAM, Verdict.PRESENT),
    ]
    state, _ = pipeline.reconcile_claim(rows)
    assert state == "visually_supported"


def test_reconcile_supported_via_recheck(listing, photos):
    job = _running_job(listing)
    rows = [
        _obs_row(job, photos[0], ObservationSource.Muse, Verdict.UNCLEAR),
        _obs_row(job, photos[0], ObservationSource.SAM, Verdict.ABSENT),
        _obs_row(job, photos[0], ObservationSource.RECHECK, Verdict.PRESENT),
    ]
    state, _ = pipeline.reconcile_claim(rows)
    assert state == "visually_supported"


# NOTE: needs_other_view previously asserted NOT_VISUALLY_VERIFIABLE here.
# That was the Finding 2 defect: needing another photograph is still a
# visual question. Coverage moved to
# test_conservative_ambiguous_housing_needs_photo (insufficient) and
# test_conservative_nonvisual_claim (intrinsically nonvisual claims only).


def test_reconcile_insufficient_on_disagreement(listing, photos):
    job = _running_job(listing)
    rows = [
        _obs_row(job, photos[0], ObservationSource.Muse, Verdict.PRESENT),
        _obs_row(job, photos[0], ObservationSource.SAM, Verdict.ABSENT),
    ]
    state, _ = pipeline.reconcile_claim(rows)
    assert state == "insufficient_evidence"


def test_reconcile_never_verified(listing, photos):
    job = _running_job(listing)
    rows = [
        _obs_row(job, photos[0], ObservationSource.Muse, Verdict.PRESENT),
        _obs_row(job, photos[0], ObservationSource.SAM, Verdict.PRESENT),
        _obs_row(job, photos[0], ObservationSource.RECHECK, Verdict.PRESENT),
    ]
    state, _ = pipeline.reconcile_claim(rows)
    assert state != "verified"


def _conservative(job, photos, specs, claim="adaptive cruise control"):
    rows = [
        _obs_row(job, photos[photo], source, verdict, claim, **extra)
        for photo, source, verdict, extra in specs
    ]
    return pipeline.reconcile_claim(rows, claim)


def test_conservative_no_radar_in_exteriors(listing, photos):
    """Absence in exterior shots is not a contradiction (Rule E)."""
    job = _running_job(listing)
    M = ObservationSource.Muse
    state, _ = _conservative(
        job,
        photos,
        [(0, M, Verdict.ABSENT, {}), (1, M, Verdict.ABSENT, {})],
    )
    assert state == "insufficient_evidence"


def test_conservative_buttons_without_confirmed_acc(listing, photos):
    """Buttons spotted but SAM cannot localize ACC: unconfirmed."""
    job = _running_job(listing)
    M, S = ObservationSource.Muse, ObservationSource.SAM
    state, _ = _conservative(
        job,
        photos,
        [(0, M, Verdict.PRESENT, {}), (0, S, Verdict.ABSENT, {})],
    )
    assert state == "insufficient_evidence"


def test_conservative_no_relevant_photos(listing, photos):
    job = _running_job(listing)
    M = ObservationSource.Muse
    state, _ = _conservative(
        job,
        photos,
        [(0, M, Verdict.UNCLEAR, {}), (1, M, Verdict.UNCLEAR, {})],
    )
    assert state == "insufficient_evidence"


def test_conservative_irrelevant_photos(listing, photos):
    job = _running_job(listing)
    M = ObservationSource.Muse
    state, _ = _conservative(
        job,
        photos,
        [(0, M, Verdict.ABSENT, {}), (1, M, Verdict.ABSENT, {})],
        claim="night vision assist",
    )
    assert state == "insufficient_evidence"


def test_conservative_positive_plus_inconclusive(listing, photos):
    """One unconfirmed sighting among inconclusive views decides nothing."""
    job = _running_job(listing)
    M, S = ObservationSource.Muse, ObservationSource.SAM
    state, _ = _conservative(
        job,
        photos,
        [
            (0, M, Verdict.PRESENT, {}),
            (1, M, Verdict.UNCLEAR, {}),
            (0, S, Verdict.UNCLEAR, {}),
        ],
    )
    assert state == "insufficient_evidence"


def test_conservative_conflicting_observations(listing, photos):
    job = _running_job(listing)
    M = ObservationSource.Muse
    state, _ = _conservative(
        job,
        photos,
        [(0, M, Verdict.PRESENT, {}), (1, M, Verdict.ABSENT, {})],
    )
    assert state == "insufficient_evidence"


def test_conservative_authoritative_discrepancy(listing, photos):
    """Only authoritative vehicle-specific evidence can contradict/verify."""
    job = _running_job(listing)
    A = ObservationSource.AUTHORITATIVE
    state, summary = _conservative(job, photos, [(0, A, Verdict.ABSENT, {})])
    assert state == "contradicted"
    assert "authoritative" in summary
    state, _ = _conservative(
        job, photos, [(0, A, Verdict.PRESENT, {})], claim="lane keeping assist"
    )
    assert state == "verified"


def test_conservative_nonvisual_claim(listing, photos):
    job = _running_job(listing)
    M = ObservationSource.Muse
    state, _ = _conservative(
        job,
        photos,
        [(0, M, Verdict.UNCLEAR, {}), (1, M, Verdict.UNCLEAR, {})],
        claim="driver assistance software version 3.2",
    )
    assert state == "not_visually_verifiable"
    # Photo evidence can still confirm even an intrinsically nonvisual claim.
    state, _ = _conservative(
        job,
        photos,
        [
            (0, M, Verdict.PRESENT, {}),
            (0, ObservationSource.SAM, Verdict.PRESENT, {}),
        ],
        claim="firmware update 2.1",
    )
    assert state == "visually_supported"


def test_conservative_lane_centering_not_borrowed():
    """Lane-keeping evidence must not resolve a lane-centering claim."""
    state, _ = pipeline.reconcile_claim([], "lane centering")
    assert state == "insufficient_evidence"


def test_conservative_ambiguous_housing_needs_photo(listing, photos):
    """Needing another photograph is still a visual question, not a
    nonvisual one."""
    job = _running_job(listing)
    M = ObservationSource.Muse
    state, _ = _conservative(
        job,
        photos,
        [(0, M, Verdict.UNCLEAR, {"needs_other_view": True})],
    )
    assert state == "insufficient_evidence"


def test_run_reconcile_persists_findings_and_completes(listing, photos):
    from audits.models import AuditFinding

    job = _running_job(listing)
    _obs_row(job, photos[0], ObservationSource.Muse, Verdict.ABSENT, claim="c1")
    _obs_row(job, photos[1], ObservationSource.Muse, Verdict.ABSENT, claim="c1")
    assert pipeline.run_reconcile(job.pk, "w1") == 2
    findings = {
        (f.claim_key, f.claim_text): f.state
        for f in AuditFinding.objects.filter(job=job)
    }
    assert findings == {
        ("c1", "adaptive cruise control"): "insufficient_evidence",
        ("c2", "lane keeping assist"): "insufficient_evidence",
    }
    assert AuditFinding.objects.get(job=job, claim_key="c1").evidence["rule"] == "v4"
    job.refresh_from_db()
    assert job.status == JobStatus.COMPLETE


# --- snapshot + conditional recheck (Finding 7) ---------------------------


def _queued_running_job(listing):
    job, _ = services.queue_audit(listing.pk)
    claimed = services.claim_job("w1")
    assert claimed is not None and claimed.pk == job.pk
    return claimed


def test_snapshot_freezes_claims(listing, photos, enabled):
    job = _queued_running_job(listing)
    assert [c["text"] for c in job.snapshot["claims"]] == [
        "adaptive cruise control",
        "lane keeping assist",
    ]
    listing.advertised_claims = ["night vision assist"]
    listing.save()
    muse = ScriptedMuseAdapter(
        [
            [
                _obs(str(photos[0].id), "c1"),
                _obs(str(photos[0].id), "c2", visible_object="none"),
            ],
            [
                _obs(str(photos[1].id), "c1"),
                _obs(str(photos[1].id), "c2", visible_object="none"),
            ],
        ]
    )
    done = pipeline.run_stages(job.pk, "w1", muse, FakeSamAdapter(records=[]))
    assert done.status == JobStatus.COMPLETE
    from audits.models import AuditFinding

    keys = sorted(
        AuditFinding.objects.filter(job=job).values_list("claim_key", flat=True)
    )
    assert keys == ["c1", "c2"]


def test_snapshot_ignores_late_photos(listing, photos, enabled):
    from evidence import images

    from .conftest import make_image_bytes

    job = _queued_running_job(listing)
    late, _ = images.store_listing_photo(
        listing,
        data=make_image_bytes("PNG", "LATE"),
        filename="late.png",
        source_url=None,
        provenance="extension",
    )
    screened = pipeline.run_screening(job.pk, "w1")
    assert {str(p.pk) for p in screened.photos} == {str(p.pk) for p in photos}
    assert str(late.pk) not in {str(p.pk) for p in screened.photos}


def test_snapshot_missing_evidence_fails(listing, photos, enabled):
    # Queue first (snapshot has both photos), then remove one row.
    job, _ = services.queue_audit(listing.pk)
    assert len(job.snapshot["photos"]) == 2
    from evidence.models import Photograph

    Photograph.objects.filter(pk=photos[1].pk).delete()
    claimed = services.claim_job("w1")
    with pytest.raises(services.JobNotRunningError):
        pipeline.run_screening(claimed.pk, "w1")
    claimed.refresh_from_db()
    assert claimed.status == JobStatus.FAILED
    assert claimed.error_code == "evidence_missing"


def test_recheck_skipped_when_confirmed(listing, photos, enabled):
    job = _running_job(listing)
    Observation.objects.create(
        job=job,
        claim_key="c1",
        photo=photos[0],
        source=ObservationSource.Muse,
        verdict=Verdict.PRESENT,
        extra={"supports_localization": True, "needs_crop": False},
    )
    Observation.objects.create(
        job=job,
        claim_key="c1",
        photo=photos[0],
        source=ObservationSource.SAM,
        verdict=Verdict.PRESENT,
        boxes=[{"box": [0, 0, 16, 16], "mask_ref": None}],
    )
    assert pipeline.run_crop(job.pk, "w1") == 1
    adapter = FakeMuseAdapter(observations=[])
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 0
    assert adapter.calls == 0


def test_recheck_runs_on_disagreement(listing, photos, enabled):
    job = _running_job(listing)
    Observation.objects.create(
        job=job,
        claim_key="c1",
        photo=photos[0],
        source=ObservationSource.Muse,
        verdict=Verdict.ABSENT,
        extra={"supports_localization": True, "needs_crop": False},
    )
    Observation.objects.create(
        job=job,
        claim_key="c1",
        photo=photos[0],
        source=ObservationSource.SAM,
        verdict=Verdict.PRESENT,
        boxes=[{"box": [0, 0, 16, 16], "mask_ref": None}],
    )
    assert pipeline.run_crop(job.pk, "w1") == 1
    adapter = FakeMuseAdapter(observations=[_obs(str(photos[0].id), "c1")])
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 1


def test_recheck_records_changed(listing, photos, enabled):
    job = _running_job(listing)
    Observation.objects.create(
        job=job,
        claim_key="c1",
        photo=photos[0],
        source=ObservationSource.Muse,
        verdict=Verdict.UNCLEAR,
        extra={"supports_localization": True, "needs_crop": True},
    )
    Observation.objects.create(
        job=job,
        claim_key="c1",
        photo=photos[0],
        source=ObservationSource.SAM,
        verdict=Verdict.PRESENT,
        boxes=[{"box": [0, 0, 16, 16], "mask_ref": None}],
    )
    assert pipeline.run_crop(job.pk, "w1") == 1
    adapter = FakeMuseAdapter(observations=[_obs(str(photos[0].id), "c1")])
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 1
    obs = Observation.objects.get(
        job=job, claim_key="c1", source=ObservationSource.RECHECK
    )
    assert obs.verdict == Verdict.PRESENT
    assert obs.extra["changed"] is True


def test_reconcile_records_recheck_influence(listing, photos):
    from audits.models import AuditFinding

    job = _running_job(listing)
    _obs_row(job, photos[0], ObservationSource.Muse, Verdict.UNCLEAR, claim="c1")
    _obs_row(job, photos[0], ObservationSource.SAM, Verdict.ABSENT, claim="c1")
    _obs_row(job, photos[0], ObservationSource.RECHECK, Verdict.PRESENT, claim="c1")
    pipeline.run_reconcile(job.pk, "w1")
    finding = AuditFinding.objects.get(job=job, claim_key="c1")
    assert finding.state == "visually_supported"
    assert finding.evidence["recheck_changed_finding"] is True
    # Without the recheck the claim would be insufficient.
    calm = AuditFinding.objects.get(job=job, claim_key="c2")
    assert calm.evidence["recheck_changed_finding"] is False


def test_history_survives_reaudit(listing, photos, enabled):
    from audits.models import AuditFinding

    job = _queued_running_job(listing)
    batch = [
        [
            _obs(str(photos[0].id), "c1"),
            _obs(str(photos[0].id), "c2", visible_object="none"),
        ],
        [
            _obs(str(photos[1].id), "c1", visible_object="none"),
            _obs(str(photos[1].id), "c2", visible_object="none"),
        ],
    ]
    pipeline.run_stages(
        job.pk, "w1", ScriptedMuseAdapter(batch), FakeSamAdapter(records=[])
    )
    first = {f.claim_key: f.state for f in AuditFinding.objects.filter(job=job)}
    job2 = _queued_running_job(listing)
    assert job2.pk != job.pk
    pipeline.run_stages(
        job2.pk, "w1", ScriptedMuseAdapter(batch), FakeSamAdapter(records=[])
    )
    again = {f.claim_key: f.state for f in AuditFinding.objects.filter(job=job)}
    assert again == first


def test_crop_links_source_observation(listing, photos, enabled):
    from audits.models import AuditCrop

    job = _running_job(listing)
    sam_obs = Observation.objects.create(
        job=job,
        claim_key="c1",
        photo=photos[0],
        source=ObservationSource.SAM,
        verdict=Verdict.PRESENT,
        boxes=[{"box": [0, 0, 16, 16], "mask_ref": None}],
    )
    assert pipeline.run_crop(job.pk, "w1") == 1
    crop = AuditCrop.objects.get(job=job)
    assert crop.source_observation_id == sam_obs.pk
    assert crop.source_photo_id == photos[0].pk


# --- Muse batching + completeness (Finding 6) ------------------------------


class ScriptedMuseAdapter(FakeMuseAdapter):
    """Per-call scripted responses (observations list or exception)."""

    def __init__(self, scripts, cost=0.0123):
        super().__init__(observations=[], cost=cost)
        self._scripts = list(scripts)
        self.requests = []

    def analyze(self, request):
        self.requests.append(request)
        script = self._scripts.pop(0) if self._scripts else []
        if isinstance(script, Exception):
            self.calls += 1
            raise script
        self._observations = script
        return super().analyze(request)


def _batch_obs(photo_id, cid, **overrides):
    return _obs(photo_id, cid, **overrides)


def test_muse_batches_one_photo_per_call(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter(
        [
            [
                _batch_obs(str(photos[0].id), "c1"),
                _batch_obs(str(photos[0].id), "c2"),
            ],
            [
                _batch_obs(str(photos[1].id), "c1"),
                _batch_obs(str(photos[1].id), "c2"),
            ],
        ]
    )
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 4
    assert adapter.calls == 2
    assert adapter.requests[0].photo_ids == (str(photos[0].id),)
    assert adapter.requests[1].photo_ids == (str(photos[1].id),)
    assert "c1: adaptive cruise control" in adapter.requests[0].user_text


def test_muse_chunks_large_claim_sets(enabled):
    from evidence import images

    from .conftest import make_image_bytes

    listing = Listing.objects.create(
        source_url="https://example.test/l/many-claims",
        source_site="example.test",
        title="many claims",
        advertised_claims=[f"feature {i}" for i in range(9)],
    )
    photo, _ = images.store_listing_photo(
        listing,
        data=make_image_bytes("PNG", "MC"),
        filename="mc.png",
        source_url=None,
        provenance="extension",
    )
    job = _running_job(listing)
    # Snapshot backfill covers direct-created jobs.
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter(
        [
            [_batch_obs(str(photo.id), f"c{i + 1}") for i in range(6)],
            [_batch_obs(str(photo.id), f"c{i + 7}") for i in range(3)],
        ]
    )
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 9
    assert adapter.calls == 2
    assert adapter.requests[0].max_output_tokens == 4096


def test_muse_empty_batch_fails(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter([[]])
    with pytest.raises(ProviderResponseError):
        pipeline.run_muse(job.pk, "w1", screened, adapter)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_code == "empty_batch"
    # The billed call is still in the ledger.
    assert (
        AuditExecution.objects.filter(
            job=job, stage=Stage.Muse, provider=Provider.BASETEN
        ).count()
        == 1
    )


def test_muse_missing_pair_fails(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter(
        [[_batch_obs(str(photos[0].id), "c1")]]  # c2 never returned
    )
    with pytest.raises(ProviderResponseError):
        pipeline.run_muse(job.pk, "w1", screened, adapter)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_code == "incomplete_batch"


def test_muse_unknown_claim_fails(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter([[_batch_obs(str(photos[0].id), "c99")]])
    with pytest.raises(ProviderResponseError):
        pipeline.run_muse(job.pk, "w1", screened, adapter)
    job.refresh_from_db()
    assert job.error_code == "unknown_claim"
    assert not Observation.objects.filter(job=job).exists()


def test_muse_unknown_photo_fails(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter(
        [[_batch_obs(str(photos[1].id), "c1")]]  # batch asked for photos[0]
    )
    with pytest.raises(ProviderResponseError):
        pipeline.run_muse(job.pk, "w1", screened, adapter)
    job.refresh_from_db()
    assert job.error_code == "unknown_photo"


def test_muse_duplicates_first_wins_with_warning(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    first = _batch_obs(str(photos[0].id), "c1", description="first")
    dup = _batch_obs(str(photos[0].id), "c1", description="second")
    adapter = ScriptedMuseAdapter(
        [
            [first, dup, _batch_obs(str(photos[0].id), "c2")],
            [
                _batch_obs(str(photos[1].id), "c1"),
                _batch_obs(str(photos[1].id), "c2"),
            ],
        ]
    )
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 4
    obs = Observation.objects.get(
        job=job, claim_key="c1", photo=photos[0], source=ObservationSource.Muse
    )
    assert obs.rationale == "first"
    row = AuditExecution.objects.filter(
        job=job, stage=Stage.Muse, provider=Provider.BASETEN
    ).earliest("created_at")
    assert any("duplicate" in w for w in row.usage["validation_warnings"])


def test_muse_batch_failure_preserves_prior(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter(
        [
            [
                _batch_obs(str(photos[0].id), "c1"),
                _batch_obs(str(photos[0].id), "c2"),
            ],
            ProviderRateLimitError("slow down"),
        ]
    )
    with pytest.raises(ProviderRateLimitError):
        pipeline.run_muse(job.pk, "w1", screened, adapter)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert Observation.objects.filter(job=job).count() == 2


def test_muse_resume_skips_complete_batches(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    for cid in ("c1", "c2"):
        Observation.objects.create(
            job=job,
            claim_key=cid,
            photo=photos[0],
            source=ObservationSource.Muse,
            verdict=Verdict.PRESENT,
        )
    adapter = ScriptedMuseAdapter(
        [
            [
                _batch_obs(str(photos[1].id), "c1"),
                _batch_obs(str(photos[1].id), "c2"),
            ]
        ]
    )
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 2
    assert adapter.calls == 1
    assert adapter.requests[0].photo_ids == (str(photos[1].id),)


# --- budget reservation (Finding 4) ----------------------------------------


def test_reserve_blocks_overspend(listing):
    job = _running_job(listing, spend_usd=Decimal("1.80"))
    with pytest.raises(services.BudgetExceededError):
        services.reserve_call(job.pk, "w1", max_attempts=3, reserve_usd=Decimal("0.50"))
    job.refresh_from_db()
    assert job.status == JobStatus.PAUSED
    assert job.error_code == "spend_cap"
    assert job.model_calls_made == 0
    assert job.reserved_calls == 0


def test_reserve_blocks_retry_over_call_cap(listing):
    job = _running_job(listing, max_calls=60, model_calls_made=58)
    with pytest.raises(services.BudgetExceededError):
        services.reserve_call(job.pk, "w1", max_attempts=3, reserve_usd=Decimal("0.50"))
    job.refresh_from_db()
    assert job.status == JobStatus.PAUSED
    assert job.error_code == "call_cap"


def test_reserve_at_exact_cap_fails(listing):
    job = _running_job(listing, spend_cap_usd=Decimal("2.00"))
    job.spend_usd = Decimal("2.00")
    job.save()
    with pytest.raises(services.BudgetExceededError):
        services.reserve_call(job.pk, "w1", max_attempts=1, reserve_usd=Decimal("0.50"))
    job.refresh_from_db()
    assert job.status == JobStatus.PAUSED


def test_local_rows_ignore_model_allowance(listing):
    job = _running_job(listing, max_calls=1)
    services.record_execution(
        job.pk, "w1", stage=Stage.SCREENING, provider=Provider.LOCAL
    )
    services.record_execution(job.pk, "w1", stage=Stage.CROP, provider=Provider.LOCAL)
    job.refresh_from_db()
    assert job.calls_made == 2
    assert job.model_calls_made == 0
    assert job.status == JobStatus.RUNNING
    services.reserve_call(job.pk, "w1", max_attempts=1, reserve_usd=Decimal("0.50"))
    services.settle_call(job.pk, "w1", attempts=1, cost_usd=Decimal("0.0100"))
    job.refresh_from_db()
    assert job.model_calls_made == 1


def test_settle_unknown_cost_pauses(listing):
    job = _running_job(listing)
    services.reserve_call(job.pk, "w1", max_attempts=3, reserve_usd=Decimal("0.50"))
    services.settle_call(job.pk, "w1", attempts=2, cost_usd=None)
    job.refresh_from_db()
    assert job.status == JobStatus.PAUSED
    assert job.error_code == "unknown_cost"
    assert job.unknown_cost_calls == 1
    assert job.model_calls_made == 2
    assert job.spend_usd == Decimal("0.0000")
    assert job.reserved_calls == 0


def test_settle_books_over_terminal_status(listing):
    """A call in flight during cancel still books its spend; the cancel
    stands and no pause overwrites it."""
    job = _running_job(listing)
    services.reserve_call(job.pk, "w1", max_attempts=1, reserve_usd=Decimal("0.50"))
    services.cancel_job(job.pk)
    services.settle_call(job.pk, "w1", attempts=1, cost_usd=Decimal("0.0100"))
    job.refresh_from_db()
    assert job.status == JobStatus.CANCELLED
    assert job.spend_usd == Decimal("0.0100")
    assert job.model_calls_made == 1
    assert job.reserved_calls == 0


def test_stale_worker_cannot_reserve(listing):
    job = _running_job(listing)
    with pytest.raises(services.StaleWorkerError):
        services.reserve_call(
            job.pk, "intruder", max_attempts=1, reserve_usd=Decimal("0.50")
        )


@pytest.mark.django_db(transaction=True)
def test_concurrent_reserve_single_winner(listing):
    import threading

    job = _running_job(listing)
    barrier = threading.Barrier(6)
    won: list = []
    lost: list = []

    def _reserve() -> None:
        from django.db import connections

        try:
            barrier.wait(timeout=10)
            services.reserve_call(
                job.pk, "w1", max_attempts=1, reserve_usd=Decimal("0.50")
            )
            won.append(1)
        except services.BudgetExceededError:
            lost.append(1)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=_reserve) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(won) == 1
    assert len(lost) == 5
    job.refresh_from_db()
    assert job.status == JobStatus.RUNNING
    assert job.reserved_calls == 1
    services.settle_call(job.pk, "w1", attempts=1, cost_usd=Decimal("0.01"))
    job.refresh_from_db()
    assert job.reserved_calls == 0
    assert job.spend_usd == Decimal("0.01")
    listing.delete()


def test_classify_error_cost():
    from decimal import Decimal as D

    from providers.base import (
        ProviderAuthError,
        ProviderError,
        ProviderNotConfigured,
        ProviderRateLimitError,
        ProviderResponseError,
        ProviderTimeoutError,
    )

    assert pipeline.classify_error_cost(ProviderAuthError("no")) == D("0.0000")
    assert pipeline.classify_error_cost(ProviderNotConfigured("x")) == D("0.0000")
    assert pipeline.classify_error_cost(ProviderRateLimitError("slow")) == D("0.0000")
    assert pipeline.classify_error_cost(ProviderError("bad", status=400)) == D("0.0000")
    assert pipeline.classify_error_cost(ProviderTimeoutError("t")) is None
    assert pipeline.classify_error_cost(ProviderResponseError("m")) is None
    assert pipeline.classify_error_cost(ProviderError("s", status=500)) is None
    assert pipeline.classify_error_cost(ProviderError("t", status=408)) is None


def test_muse_never_calls_when_reserve_fails(listing, photos, enabled):
    job = _running_job(listing, spend_usd=Decimal("1.90"))
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = FakeMuseAdapter(observations=[])
    with pytest.raises(services.BudgetExceededError):
        pipeline.run_muse(job.pk, "w1", screened, adapter)
    assert adapter.calls == 0
    job.refresh_from_db()
    assert job.status == JobStatus.PAUSED
    assert job.error_code == "spend_cap"


def test_cancel_between_batches_halts_without_further_calls(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")

    class CancellingAdapter(ScriptedMuseAdapter):
        def analyze(self, request):
            result = super().analyze(request)
            services.cancel_job(job.pk)
            return result

    adapter = CancellingAdapter(
        [
            [
                _obs(str(photos[0].id), "c1"),
                _obs(str(photos[0].id), "c2", visible_object="none"),
            ],
            [
                _obs(str(photos[1].id), "c1", visible_object="none"),
                _obs(str(photos[1].id), "c2", visible_object="none"),
            ],
        ]
    )
    with pytest.raises(services.JobNotRunningError):
        pipeline.run_muse(job.pk, "w1", screened, adapter)
    assert adapter.calls == 1
    job.refresh_from_db()
    assert job.status == JobStatus.CANCELLED
    assert job.spend_usd == Decimal("0.0123")
    assert job.model_calls_made == 1
    assert job.reserved_calls == 0
    assert Observation.objects.filter(job=job).count() == 2


def test_provider_timeout_settles_unknown(listing, photos, enabled):
    from providers.base import ProviderTimeoutError

    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = FakeMuseAdapter(error=ProviderTimeoutError("hung"))
    with pytest.raises(ProviderTimeoutError):
        pipeline.run_muse(job.pk, "w1", screened, adapter)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_code == "ProviderTimeoutError"
    assert job.unknown_cost_calls == 1
    assert job.spend_usd == Decimal("0.0000")


def test_stale_settle_cannot_release_new_owner_hold(listing):
    job = _running_job(listing)
    services.reserve_call(job.pk, "w1", max_attempts=3, reserve_usd=Decimal("0.50"))
    AuditJob.objects.filter(pk=job.pk).update(
        heartbeat_at=timezone.now() - timedelta(hours=2)
    )
    stolen = services.claim_job("w2")
    assert stolen is not None
    assert stolen.worker_id == "w2"
    services.reserve_call(job.pk, "w2", max_attempts=3, reserve_usd=Decimal("0.50"))
    # The merely-slow first worker settles late: its spend books, but
    # the new owner's reservation survives.
    services.settle_call(job.pk, "w1", attempts=1, cost_usd=Decimal("0.0100"), ok=False)
    job.refresh_from_db()
    assert job.worker_id == "w2"
    assert job.spend_usd == Decimal("0.0100")
    assert job.reserved_calls == 3
    assert job.status == JobStatus.RUNNING


def test_auth_error_settles_zero(listing, photos, enabled):
    from providers.base import ProviderAuthError

    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = FakeMuseAdapter(error=ProviderAuthError("bad key"))
    with pytest.raises(ProviderAuthError):
        pipeline.run_muse(job.pk, "w1", screened, adapter)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.spend_usd == Decimal("0.0000")
    assert job.unknown_cost_calls == 0
    assert job.model_calls_made >= 1


def test_provider_costs_attributable_by_pool(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    muse = ScriptedMuseAdapter(
        [
            [
                _obs(str(photos[0].id), "c1"),
                _obs(str(photos[0].id), "c2", visible_object="none"),
            ],
            [
                _obs(str(photos[1].id), "c1", visible_object="none"),
                _obs(str(photos[1].id), "c2", visible_object="none"),
            ],
        ],
        cost=0.0123,
    )
    pipeline.run_muse(job.pk, "w1", screened, muse)
    sam = FakeSamAdapter(records=[_segment()], cost=0.0025)
    pipeline.run_sam(job.pk, "w1", sam)
    totals: dict[str, Decimal] = {}
    for row in AuditExecution.objects.filter(job=job).exclude(cost_usd__isnull=True):
        totals[row.provider] = totals.get(row.provider, Decimal(0)) + row.cost_usd
    assert totals[Provider.BASETEN] == Decimal("0.0123") * 2
    assert totals[Provider.META] == Decimal("0.0025")
    job.refresh_from_db()
    assert job.spend_usd == sum(totals.values(), Decimal(0))


def test_full_pipeline_completes_with_findings(listing, photos, enabled):
    from audits.models import AuditFinding

    listing.year = 2021
    listing.make = "Kia"
    listing.model = "Forte"
    listing.trim = "GT-Line"
    listing.save(update_fields=["year", "make", "model", "trim"])
    job = _running_job(listing)
    acc_button = {
        "visible_object": "following-distance button",
        "control_symbol": "vehicle with distance bars",
        "control_type": "button",
        "location": "steering_wheel",
        "dedicated": True,
    }
    muse = ScriptedMuseAdapter(
        [
            [
                _obs(str(photos[0].id), "c1", **acc_button),
                _obs(str(photos[0].id), "c2", visible_object="none"),
            ],
            [
                _obs(str(photos[1].id), "c1", **acc_button),
                _obs(str(photos[1].id), "c2", visible_object="none"),
            ],
        ]
    )
    sam = FakeSamAdapter(records=[_segment()])
    done = pipeline.run_stages(job.pk, "w1", muse, sam)
    assert done.status == JobStatus.COMPLETE
    findings = {
        (f.claim_key, f.claim_text): f.state
        for f in AuditFinding.objects.filter(job=job)
    }
    assert findings == {
        ("c1", "adaptive cruise control"): "visually_supported",
        ("c2", "lane keeping assist"): "insufficient_evidence",
    }
    acc = AuditFinding.objects.get(job=job, claim_key="c1")
    assert acc.evidence["interpretation"]["entry_id"] == ("kia-forte-2021-scc-distance")
    assert acc.evidence["interpretation"]["strength"] == "diagnostic"
    sam_obs = Observation.objects.filter(job=job, source=ObservationSource.SAM).first()
    assert sam_obs.extra["sam_target"] == "following-distance button"
    assert sam_obs.extra["target_source"] == "observed_object"
    stages = list(
        AuditExecution.objects.filter(job=job)
        .order_by("created_at")
        .values_list("stage", flat=True)
    )
    assert stages == [
        "screening",
        "muse",
        "muse",
        "sam",
        "sam",
        "crop",
        "recheck",
        "reconcile",
    ]
    done.refresh_from_db()
    assert done.spend_usd > Decimal("0.0000")
    assert done.unknown_cost_calls == 0
    # Ledger math: job spend equals the sum of known execution costs.
    ledger_sum = sum(
        (
            e.cost_usd
            for e in AuditExecution.objects.filter(job=job).exclude(
                cost_usd__isnull=True
            )
        ),
        Decimal("0.0000"),
    )
    assert done.spend_usd == ledger_sum


# --- standalone worker lifecycle (F3) ------------------------------------


def _two_photo_scripts(photos):
    return [
        [
            _obs(str(photos[0].id), "c1"),
            _obs(str(photos[0].id), "c2", visible_object="none"),
        ],
        [
            _obs(str(photos[1].id), "c1"),
            _obs(str(photos[1].id), "c2", visible_object="none"),
        ],
    ]


def test_worker_restart_reclaims_and_resumes(listing, photos, enabled):
    from audits import worker

    job, created = services.queue_audit(listing.pk, requested_by="operator")
    assert created
    services.claim_job("w-old")
    screened = pipeline.run_screening(job.pk, "w-old")
    first = ScriptedMuseAdapter(_two_photo_scripts(photos))
    assert pipeline.run_muse(job.pk, "w-old", screened, first) == 4
    assert first.calls == 2
    before = set(Observation.objects.filter(job=job).values_list("pk", flat=True))
    assert len(before) == 4
    # Crash: the lease goes stale with Muse results durable but no
    # SAM/reconcile work done.
    AuditJob.objects.filter(pk=job.pk).update(
        heartbeat_at=timezone.now() - timedelta(hours=2)
    )
    resumed_muse = ScriptedMuseAdapter([])
    resumed_sam = FakeSamAdapter(records=[_segment()])
    settled = worker.run_claimed_job("w-new", resumed_muse, resumed_sam)
    assert settled is not None
    assert settled.status == JobStatus.COMPLETE
    assert settled.worker_id == "w-new"
    # Completed Muse batches are never re-fired after the restart.
    assert resumed_muse.calls == 0
    after = set(Observation.objects.filter(job=job).values_list("pk", flat=True))
    assert before < after
    assert settled.findings.count() == 2


def test_worker_refuses_while_inference_disabled(listing, enabled, settings):
    from audits import worker

    job, _ = services.queue_audit(listing.pk, requested_by="operator")
    settings.AUDIT_INFERENCE_ENABLED = False
    assert worker.run_claimed_job("w-x") is None
    job.refresh_from_db()
    assert job.status == JobStatus.QUEUED
    assert job.worker_id == ""


def test_completed_job_never_reexecutes(listing, photos, enabled):
    from audits import worker

    services.queue_audit(listing.pk, requested_by="operator")
    muse = ScriptedMuseAdapter(_two_photo_scripts(photos))
    sam = FakeSamAdapter(records=[_segment()])
    settled = worker.run_claimed_job("w-first", muse, sam)
    assert settled is not None
    assert settled.status == JobStatus.COMPLETE
    executions = AuditExecution.objects.filter(job_id=settled.pk).count()
    findings = list(
        settled.findings.order_by("claim_key").values_list("claim_key", "state")
    )
    idle_muse = ScriptedMuseAdapter(_two_photo_scripts(photos))
    idle_sam = FakeSamAdapter(records=[_segment()])
    assert worker.run_claimed_job("w-late", idle_muse, idle_sam) is None
    assert idle_muse.calls == 0
    assert idle_sam.calls == 0
    settled.refresh_from_db()
    assert AuditExecution.objects.filter(job_id=settled.pk).count() == executions
    assert (
        list(settled.findings.order_by("claim_key").values_list("claim_key", "state"))
        == findings
    )


def test_cancelled_queued_job_never_claimed(listing, enabled):
    from audits import worker

    job, _ = services.queue_audit(listing.pk, requested_by="operator")
    assert services.cancel_job(job.pk) is True
    assert worker.run_claimed_job("w-x") is None
    job.refresh_from_db()
    assert job.status == JobStatus.CANCELLED


def test_stale_worker_cannot_overwrite_completed(listing, photos, enabled):
    from audits import worker

    services.queue_audit(listing.pk, requested_by="operator")
    muse = ScriptedMuseAdapter(_two_photo_scripts(photos))
    sam = FakeSamAdapter(records=[_segment()])
    settled = worker.run_claimed_job("w-first", muse, sam)
    assert settled is not None
    assert settled.status == JobStatus.COMPLETE
    before = list(
        settled.findings.order_by("claim_key").values_list(
            "claim_key", "state", "summary"
        )
    )
    with pytest.raises(services.StaleWorkerError):
        pipeline.run_stages(settled.pk, "w-intruder", muse, sam)
    settled.refresh_from_db()
    assert settled.status == JobStatus.COMPLETE
    assert (
        list(
            settled.findings.order_by("claim_key").values_list(
                "claim_key", "state", "summary"
            )
        )
        == before
    )


def test_run_audit_worker_once_processes_job(listing, photos, enabled, monkeypatch):
    import io

    from django.core.management import call_command

    muse = ScriptedMuseAdapter(_two_photo_scripts(photos))
    sam = FakeSamAdapter(records=[_segment()])
    monkeypatch.setattr(pipeline, "BasetenMuseAdapter", lambda model_id="": muse)
    monkeypatch.setattr(pipeline, "MetaSamAdapter", lambda model_id="": sam)
    services.queue_audit(listing.pk, requested_by="operator")
    out = io.StringIO()
    call_command("run_audit_worker", once=True, worker_id="w-cmd", stdout=out)
    job = AuditJob.objects.get(listing=listing)
    assert job.status == JobStatus.COMPLETE
    assert job.worker_id == "w-cmd"
    assert muse.calls == 2
    assert "w-cmd" in out.getvalue()


def test_run_audit_worker_once_empty_queue(enabled):
    import io

    from django.core.management import call_command

    out = io.StringIO()
    call_command("run_audit_worker", once=True, stdout=out)
    assert "started" in out.getvalue()


# --- stale-worker evidence fencing (remediation F1) ------------------------


class _StealingMuseAdapter(ScriptedMuseAdapter):
    """Deterministic mid-call lease loss: the first analyze() ages the
    heartbeat and lets worker B reclaim, then returns a valid response."""

    def __init__(self, scripts, job_pk, cost=0.0123):
        super().__init__(scripts, cost=cost)
        self._job_pk = job_pk
        self.stolen = False

    def analyze(self, request):
        if not self.stolen:
            self.stolen = True
            AuditJob.objects.filter(pk=self._job_pk).update(
                heartbeat_at=timezone.now() - timedelta(hours=2)
            )
            assert services.claim_job("w-B") is not None
        return super().analyze(request)


class _StealingSamAdapter(FakeSamAdapter):
    def __init__(self, job_pk, **kwargs):
        super().__init__(**kwargs)
        self._job_pk = job_pk
        self.stolen = False

    def analyze(self, request):
        if not self.stolen:
            self.stolen = True
            AuditJob.objects.filter(pk=self._job_pk).update(
                heartbeat_at=timezone.now() - timedelta(hours=2)
            )
            assert services.claim_job("w-B") is not None
        return super().analyze(request)


class _StealingRecheckAdapter(FakeMuseAdapter):
    def __init__(self, job_pk, **kwargs):
        super().__init__(**kwargs)
        self._job_pk = job_pk
        self.stolen = False

    def analyze(self, request):
        if not self.stolen:
            self.stolen = True
            AuditJob.objects.filter(pk=self._job_pk).update(
                heartbeat_at=timezone.now() - timedelta(hours=2)
            )
            assert services.claim_job("w-B") is not None
        return super().analyze(request)


def test_fence_allows_owner_rejects_strangers(listing):
    job = _running_job(listing, worker="w-A")
    services.fence_evidence_write(job.pk, "w-A")
    with pytest.raises(services.StaleWorkerError):
        services.fence_evidence_write(job.pk, "w-intruder")
    # The owner may persist an already-paid call through its own pause or
    # cancel (unclaimable; the run halts right after). A non-owner never
    # writes, whatever the status.
    services.cancel_job(job.pk)
    services.fence_evidence_write(job.pk, "w-A")
    with pytest.raises(services.StaleWorkerError):
        services.fence_evidence_write(job.pk, "w-intruder")
    AuditJob.objects.filter(pk=job.pk).update(status=JobStatus.FAILED)
    with pytest.raises(services.JobNotRunningError):
        services.fence_evidence_write(job.pk, "w-A")


def test_stale_muse_keeps_ledger_loses_evidence(listing, photos, enabled):
    job = _running_job(listing, worker="w-A")
    screened = pipeline.run_screening(job.pk, "w-A")
    thief = _StealingMuseAdapter(
        [
            [
                _obs(str(photos[0].id), "c1"),
                _obs(str(photos[0].id), "c2"),
            ],
        ],
        job.pk,
    )
    with pytest.raises(services.StaleWorkerError):
        pipeline.run_muse(job.pk, "w-A", screened, thief)
    job.refresh_from_db()
    assert job.status == JobStatus.RUNNING
    assert job.worker_id == "w-B"
    # Money is real even for the stale worker: spend stays booked.
    assert job.spend_usd == Decimal("0.0123")
    assert job.model_calls_made == 1
    assert job.reserved_calls == 0
    assert (
        AuditExecution.objects.filter(
            job=job, stage=Stage.Muse, provider=Provider.BASETEN
        ).count()
        == 1
    )
    # But no accepted evidence from the stale worker.
    assert Observation.objects.filter(job=job).count() == 0
    # Worker B completes cleanly with its own observations.
    clean = ScriptedMuseAdapter(
        [
            [
                _obs(str(photos[0].id), "c1", visible_object="none"),
                _obs(str(photos[0].id), "c2", visible_object="none"),
            ],
            [
                _obs(str(photos[1].id), "c1", visible_object="none"),
                _obs(str(photos[1].id), "c2", visible_object="none"),
            ],
        ]
    )
    done = pipeline.run_stages(job.pk, "w-B", clean, FakeSamAdapter(records=[]))
    assert done.status == JobStatus.COMPLETE
    verdicts = {
        o.verdict
        for o in Observation.objects.filter(job=job, source=ObservationSource.Muse)
    }
    assert verdicts == {Verdict.ABSENT}


def test_stale_sam_leaves_no_masks_or_rows(listing, photos, enabled):
    job = _running_job(listing, worker="w-A")
    _obs_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        claim="c1",
        supports_localization=True,
    )
    thief = _StealingSamAdapter(job.pk, records=[_segment()])
    with pytest.raises(services.StaleWorkerError):
        pipeline.run_sam(job.pk, "w-A", thief)
    job.refresh_from_db()
    assert job.worker_id == "w-B"
    assert job.spend_usd == Decimal("0.0025")
    assert job.model_calls_made == 1
    assert (
        Observation.objects.filter(job=job, source=ObservationSource.SAM).count() == 0
    )
    masks = list(pipeline.audit_dirs(job.pk)["masks"].glob("*.png"))
    assert masks == []
    # Worker B localizes honestly: no segments, no contamination.
    done = pipeline.run_stages(
        job.pk,
        "w-B",
        ScriptedMuseAdapter(
            [
                [_obs(str(photos[0].id), "c2", visible_object="none")],
                [
                    _obs(str(photos[1].id), "c1", visible_object="none"),
                    _obs(str(photos[1].id), "c2", visible_object="none"),
                ],
            ]
        ),
        FakeSamAdapter(records=[]),
    )
    assert done.status == JobStatus.COMPLETE
    sam_obs = Observation.objects.get(job=job, source=ObservationSource.SAM)
    assert sam_obs.verdict == Verdict.ABSENT
    assert sam_obs.boxes == []


def test_stale_recheck_persists_nothing(listing, photos, enabled):
    job = _running_job(listing, worker="w-A")
    crop = _crop_row(job, photos[0])
    thief = _StealingRecheckAdapter(
        job.pk,
        observations=[_obs(str(photos[0].id), "c1")],
    )
    with pytest.raises(services.StaleWorkerError):
        pipeline.run_recheck(job.pk, "w-A", thief)
    job.refresh_from_db()
    assert job.worker_id == "w-B"
    assert job.spend_usd == Decimal("0.0123")
    assert (
        Observation.objects.filter(job=job, source=ObservationSource.RECHECK).count()
        == 0
    )
    # Worker B's recheck lands normally.
    clean = FakeMuseAdapter(
        observations=[_obs(str(photos[0].id), "c1", visible_object="none")]
    )
    assert pipeline.run_recheck(job.pk, "w-B", clean) == 1
    obs = Observation.objects.get(job=job, source=ObservationSource.RECHECK)
    assert (obs.claim_key, obs.verdict) == ("c1", Verdict.ABSENT)
    assert obs.source_crop_id == crop.pk


# --- snapshot integrity verification (remediation F3) ----------------------


def _tamper_original(photo):
    from evidence.images import original_path

    path = original_path(photo.storage_ref, photo.image_format)
    path.write_bytes(path.read_bytes() + b"tampered")


def test_screening_rejects_tampered_bytes(listing, photos, enabled):
    job = _running_job(listing)
    before = pipeline.get_snapshot(job)["photos"][0]["sha256"]
    _tamper_original(photos[0])
    with pytest.raises(services.JobNotRunningError):
        pipeline.run_screening(job.pk, "w1")
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_code == "evidence_tampered"
    # The snapshot is frozen truth: never rewritten, never "repaired".
    assert pipeline.get_snapshot(job)["photos"][0]["sha256"] == before


def test_screening_rejects_metadata_drift(listing, photos, enabled):
    from evidence.models import Photograph

    job = _running_job(listing)
    pipeline.get_snapshot(job)
    Photograph.objects.filter(pk=photos[0].pk).update(width=9999)
    with pytest.raises(services.JobNotRunningError):
        pipeline.run_screening(job.pk, "w1")
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error_code == "evidence_metadata_drift"


def test_screening_rejects_db_sha_mismatch(listing, photos, enabled):
    from evidence.models import Photograph

    job = _running_job(listing)
    pipeline.get_snapshot(job)
    Photograph.objects.filter(pk=photos[0].pk).update(sha256="0" * 64)
    with pytest.raises(services.JobNotRunningError):
        pipeline.run_screening(job.pk, "w1")
    job.refresh_from_db()
    assert job.error_code == "evidence_metadata_drift"


def test_tampered_evidence_blocks_all_provider_calls(listing, photos, enabled):
    job = _running_job(listing)
    pipeline.get_snapshot(job)
    _tamper_original(photos[1])
    muse = ScriptedMuseAdapter(_two_photo_scripts(photos))
    sam = FakeSamAdapter(records=[_segment()])
    with pytest.raises(services.JobNotRunningError):
        pipeline.run_stages(job.pk, "w1", muse, sam)
    assert muse.calls == 0
    assert sam.calls == 0
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.spend_usd == Decimal("0.0000")


def test_verified_read_rejects_midrun_tamper(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    _tamper_original(photos[0])
    muse = ScriptedMuseAdapter(_two_photo_scripts(photos))
    with pytest.raises(services.JobNotRunningError):
        pipeline.run_muse(job.pk, "w1", screened, muse)
    assert muse.calls == 0
    job.refresh_from_db()
    assert job.error_code == "evidence_tampered"


def test_restart_reverifies_before_new_calls(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    first = ScriptedMuseAdapter(_two_photo_scripts(photos))
    assert pipeline.run_muse(job.pk, "w1", screened, first) == 4
    _tamper_original(photos[1])
    second = ScriptedMuseAdapter(_two_photo_scripts(photos))
    with pytest.raises(services.JobNotRunningError):
        pipeline.run_stages(job.pk, "w1", second, FakeSamAdapter(records=[]))
    assert second.calls == 0
    job.refresh_from_db()
    assert job.error_code == "evidence_tampered"
    # Committed evidence from the verified pass survives.
    assert (
        Observation.objects.filter(job=job, source=ObservationSource.Muse).count() == 4
    )


# --- crop-scoped recheck provenance (remediation F4) -----------------------


def test_recheck_two_crops_same_claim_keep_both(listing, photos, enabled):
    job = _running_job(listing)
    first = _crop_row(job, photos[0])
    sam_obs = first.source_observation
    second = _crop_row(job, photos[0], sam_obs=sam_obs)
    adapter = ScriptedMuseAdapter(
        [
            [_obs(str(photos[0].id), "c1")],
            [_obs(str(photos[0].id), "c1")],
        ]
    )
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 2
    rows = list(Observation.objects.filter(job=job, source=ObservationSource.RECHECK))
    assert len(rows) == 2
    assert {r.source_crop_id for r in rows} == {first.pk, second.pk}
    assert rows[0].execution_id != rows[1].execution_id


def test_recheck_scoped_to_crop_claim(listing, photos, enabled):
    from audits.models import AuditCrop

    job = _running_job(listing)
    _crop_row(job, photos[0], claim="c1")
    later = _crop_row(job, photos[0], claim="c2")
    AuditCrop.objects.filter(pk=later.pk).update(created_at=timezone.now())
    adapter = ScriptedMuseAdapter(
        [
            [_obs(str(photos[0].id), "c1")],
            [_obs(str(photos[0].id), "c2", visible_object="none")],
        ]
    )
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 2
    assert len(adapter.requests) == 2
    texts = sorted(r.user_text for r in adapter.requests)
    c1_text = next(t for t in texts if "- c1:" in t)
    c2_text = next(t for t in texts if "- c2:" in t)
    assert "- c2:" not in c1_text
    assert "- c1:" not in c2_text


def test_recheck_skips_unlinked_crops(listing, photos, enabled):
    job = _running_job(listing)
    _crop_row(job, photos[0], link=False)
    adapter = FakeMuseAdapter(observations=[])
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 0
    assert adapter.calls == 0
    summary = AuditExecution.objects.filter(
        job=job, stage=Stage.RECHECK, provider=Provider.LOCAL
    ).latest("created_at")
    assert summary.usage["skipped_unlinked"] == 1


def test_recheck_disagreeing_crops_both_preserved(listing, photos, enabled):
    job = _running_job(listing)
    first = _crop_row(job, photos[0])
    second = _crop_row(job, photos[0], sam_obs=first.source_observation)
    adapter = ScriptedMuseAdapter(
        [
            [_obs(str(photos[0].id), "c1")],
            [_obs(str(photos[0].id), "c1", visible_object="none")],
        ]
    )
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 2
    rows = list(Observation.objects.filter(job=job, source=ObservationSource.RECHECK))
    assert {r.source_crop_id for r in rows} == {first.pk, second.pk}
    assert {r.verdict for r in rows} == {Verdict.PRESENT, Verdict.ABSENT}


def test_recheck_unchanged_flag_when_confirmed(listing, photos, enabled):
    job = _running_job(listing)
    _obs_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        claim="c1",
        supports_localization=True,
        needs_crop=True,
    )
    _crop_row(job, photos[0])
    adapter = FakeMuseAdapter(observations=[_obs(str(photos[0].id), "c1")])
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 1
    obs = Observation.objects.get(job=job, source=ObservationSource.RECHECK)
    assert obs.verdict == Verdict.PRESENT
    assert obs.extra["changed"] is False


def test_recheck_resume_between_crops(listing, photos, enabled):
    from audits.models import AuditCrop

    job = _running_job(listing)
    _crop_row(job, photos[0])
    later = _crop_row(job, photos[1])
    AuditCrop.objects.filter(pk=later.pk).update(created_at=timezone.now())
    first = ScriptedMuseAdapter(
        [[_obs(str(photos[0].id), "c1")], [_obs(str(photos[1].id), "c1")]]
    )
    assert pipeline.run_recheck(job.pk, "w1", first) == 2
    # A later pass (e.g. after a worker restart) re-fires nothing.
    second = ScriptedMuseAdapter([])
    assert pipeline.run_recheck(job.pk, "w1", second) == 0
    assert second.calls == 0
    assert (
        Observation.objects.filter(job=job, source=ObservationSource.RECHECK).count()
        == 2
    )


def test_recheck_per_crop_spend_attribution(listing, photos, enabled):
    job = _running_job(listing)
    first = _crop_row(job, photos[0])
    second = _crop_row(job, photos[0], sam_obs=first.source_observation)
    adapter = ScriptedMuseAdapter(
        [
            [_obs(str(photos[0].id), "c1")],
            [_obs(str(photos[0].id), "c1")],
        ],
        cost=0.01,
    )
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 2
    rows = list(
        AuditExecution.objects.filter(
            job=job, stage=Stage.RECHECK, provider=Provider.BASETEN
        )
    )
    assert len(rows) == 2
    assert {r.usage["crop_id"] for r in rows} == {str(first.pk), str(second.pk)}
    job.refresh_from_db()
    assert job.spend_usd == Decimal("0.02")
    assert sum((r.cost_usd for r in rows), Decimal(0)) == job.spend_usd


def test_recheck_legacy_row_keeps_pair_first_wins(listing, photos, enabled):
    job = _running_job(listing)
    crop = _crop_row(job, photos[0])
    Observation.objects.create(
        job=job,
        claim_key="c1",
        photo=photos[0],
        source=ObservationSource.RECHECK,
        verdict=Verdict.PRESENT,
        extra={"crop_ref": crop.storage_ref},
    )
    adapter = FakeMuseAdapter(observations=[])
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 0
    assert adapter.calls == 0


# --- manufacturer-aware ADAS recognition (remediation F2) ------------------


FORTE_2021 = {"year": 2021, "make": "Kia", "model": "Forte", "trim": "GT-Line"}
CAMRY_2020 = {"year": 2020, "make": "Toyota", "model": "Camry", "trim": "SE"}
JETTA_2022 = {"year": 2022, "make": "Volkswagen", "model": "Jetta", "trim": ""}


def _keyed_row(job, photo, source, verdict, claim="c1", **control):
    extra = {"visible_object": control.pop("visible_object", "control")}
    extra.update(control)
    return Observation.objects.create(
        job=job,
        claim_key=claim,
        photo=photo,
        source=source,
        verdict=verdict,
        extra=extra,
    )


def test_reconcile_diagnostic_acc_without_sam(listing, photos):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="following-distance button",
        control_symbol="vehicle with distance bars",
        location="steering_wheel",
        dedicated=True,
    )
    state, summary = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)),
        "adaptive cruise control",
        FORTE_2021,
    )
    assert state == "visually_supported"
    assert "diagnostic" in summary
    assert "kia-forte-2021-scc-distance" in summary
    assert "SAM localization unavailable" in summary


def test_reconcile_generic_cruise_with_sam_insufficient(listing, photos):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="cruise set and resume buttons",
        location="steering_wheel",
    )
    _obs_row(job, photos[0], ObservationSource.SAM, Verdict.PRESENT, claim="c1")
    state, summary = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)), "adaptive cruise control"
    )
    assert state == "insufficient_evidence"
    assert "not to the claimed adaptive_cruise_control" in summary


def test_reconcile_lka_supported_centering_not(listing, photos):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="lane keeping assist button",
        control_symbol="vehicle between lane lines",
        location="left_dashboard",
        dedicated=True,
        manufacturer_context="Kia Forte",
    )
    rows = list(Observation.objects.filter(job=job))
    state, _ = pipeline.reconcile_claim(rows, "lane keeping assist", FORTE_2021)
    assert state == "visually_supported"
    state, summary = pipeline.reconcile_claim(rows, "lane centering", FORTE_2021)
    assert state == "insufficient_evidence"
    assert "lane_centering_assist" in summary


def test_reconcile_stalk_menu_insufficient(listing, photos):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="driver assistance button",
        control_type="stalk button",
        location="stalk",
    )
    _obs_row(job, photos[0], ObservationSource.SAM, Verdict.PRESENT, claim="c1")
    state, summary = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)), "lane centering", JETTA_2022
    )
    assert state == "insufficient_evidence"
    assert "general assistance control" in summary


def test_reconcile_unmapped_control_insufficient(listing, photos):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="chrome door handle",
    )
    state, summary = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)), "adaptive cruise control"
    )
    assert state == "insufficient_evidence"
    assert "matches no cataloged control" in summary


def test_reconcile_legacy_rows_keep_v2_confirm(listing, photos):
    job = _running_job(listing)
    _obs_row(job, photos[0], ObservationSource.Muse, Verdict.PRESENT, claim="c1")
    _obs_row(job, photos[0], ObservationSource.SAM, Verdict.PRESENT, claim="c1")
    state, summary = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)), "adaptive cruise control"
    )
    assert state == "visually_supported"
    assert "confirmed by SAM localization" in summary


def _sam_row(job, photo, verdict, claim="c1", **extra):
    return Observation.objects.create(
        job=job,
        claim_key=claim,
        photo=photo,
        source=ObservationSource.SAM,
        verdict=verdict,
        extra=extra,
    )


def test_sam_corroboration_requires_same_photo(listing, photos):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="following-distance button",
        control_symbol="vehicle with distance bars",
        location="steering_wheel",
        dedicated=True,
    )
    _keyed_row(
        job,
        photos[1],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="cruise set and resume buttons",
        location="steering_wheel",
    )
    _sam_row(
        job,
        photos[1],
        Verdict.PRESENT,
        sam_target="cruise set and resume buttons",
        target_source="observed_object",
        control_index=0,
    )
    state, summary = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)),
        "adaptive cruise control",
        FORTE_2021,
    )
    assert state == "visually_supported"
    assert "confirmed by SAM localization" not in summary
    assert "different photo or control" in summary


def test_sam_corroboration_requires_same_control(listing, photos):
    job = _running_job(listing)
    muse_row = _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="following-distance control",
        location="steering_wheel",
        dedicated=False,
        controls=[
            {
                "visible_object": "SCC distance button",
                "control_symbol": "distance bars",
                "location": "steering_wheel",
            }
        ],
    )
    _sam_row(
        job,
        photos[0],
        Verdict.PRESENT,
        sam_target="following-distance control",
        target_source="observed_object",
        control_index=0,
        source_observation_id=str(muse_row.id),
    )
    state, summary = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)),
        "adaptive cruise control",
        FORTE_2021,
    )
    assert state == "visually_supported"
    assert "confirmed by SAM localization" not in summary
    assert "different photo or control" in summary


def test_sam_corroboration_matched_control_confirms(listing, photos):
    job = _running_job(listing)
    muse_row = _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
    )
    _sam_row(
        job,
        photos[0],
        Verdict.PRESENT,
        sam_target="following-distance button",
        target_source="observed_object",
        control_index=0,
        source_observation_id=str(muse_row.id),
    )
    state, summary = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)),
        "adaptive cruise control",
        FORTE_2021,
    )
    assert state == "visually_supported"
    assert "confirmed by SAM localization" in summary


def test_recheck_supporter_not_sam_confirmed(listing, photos):
    job = _running_job(listing)
    muse_row = _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="cruise set and resume buttons",
        location="steering_wheel",
    )
    _sam_row(
        job,
        photos[0],
        Verdict.PRESENT,
        sam_target="cruise set and resume buttons",
        target_source="observed_object",
        control_index=0,
        source_observation_id=str(muse_row.id),
    )
    _keyed_row(
        job,
        photos[0],
        ObservationSource.RECHECK,
        Verdict.PRESENT,
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
    )
    state, summary = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)),
        "adaptive cruise control",
        FORTE_2021,
    )
    assert state == "visually_supported"
    assert "confirmed by SAM localization" not in summary
    assert "different photo or control" in summary


def test_legacy_confirm_requires_same_photo(listing, photos):
    job = _running_job(listing)
    _obs_row(job, photos[0], ObservationSource.Muse, Verdict.PRESENT, claim="c1")
    _obs_row(job, photos[1], ObservationSource.SAM, Verdict.PRESENT, claim="c1")
    state, summary = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)), "adaptive cruise control"
    )
    assert state == "insufficient_evidence"
    assert "confirmed by SAM localization" not in summary
    assert "no confirmed sighting" in summary


def test_reconcile_lane_tiers_never_promote(listing, photos):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="lane keeping assist button",
        control_symbol="vehicle between lane lines",
        location="left_dashboard",
        dedicated=True,
    )
    rows = list(Observation.objects.filter(job=job))
    assert pipeline.reconcile_claim(rows, "lane departure warning", FORTE_2021)[0] == (
        "insufficient_evidence"
    )
    assert pipeline.reconcile_claim(rows, "lane following assist", FORTE_2021)[0] == (
        "insufficient_evidence"
    )
    assert pipeline.reconcile_claim(rows, "highway driving assist", FORTE_2021)[0] == (
        "insufficient_evidence"
    )
    assert pipeline.reconcile_claim(rows, "hands-free driving", FORTE_2021)[0] == (
        "insufficient_evidence"
    )


def test_reconcile_keyed_recheck_supports(listing, photos):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.RECHECK,
        Verdict.PRESENT,
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
    )
    state, _ = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)),
        "adaptive cruise control",
        FORTE_2021,
    )
    assert state == "visually_supported"


def test_snapshot_freezes_vehicle_identity(listing, photos, enabled):
    listing.year = 2021
    listing.make = "Kia"
    listing.model = "Forte"
    listing.trim = "GT-Line"
    listing.save(update_fields=["year", "make", "model", "trim"])
    job, created = services.queue_audit(listing.pk)
    assert created is True
    assert job.snapshot["vehicle"] == {
        "year": 2021,
        "make": "Kia",
        "model": "Forte",
        "trim": "GT-Line",
    }
    listing.model = "K5"
    listing.save(update_fields=["model"])
    job.refresh_from_db()
    assert job.snapshot["vehicle"]["model"] == "Forte"


def test_run_reconcile_rejects_borrowed_catalog_entry(listing, photos):
    listing.year = 2020
    listing.make = "Toyota"
    listing.model = "Camry"
    listing.save(update_fields=["year", "make", "model"])
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
    )
    assert pipeline.run_reconcile(job.pk, "w1") == 2
    finding = job.findings.get(claim_key="c1")
    # Cross-brand generalization: the borrowed manufacturer-specific
    # entry still cannot support the claim, but the capped general
    # following-distance pattern now recognizes the control at
    # visually_supported (never verified) with reviewer confirmation.
    assert finding.state == "visually_supported"
    assert "general-acc-distance-control" in finding.summary
    assert "general cross-manufacturer pattern" in finding.summary
    assert finding.evidence["interpretation"]["supports_claim"] is True
    assert finding.evidence["interpretation"]["scope"] == "general_pattern"


def test_run_reconcile_unknown_vehicle_rejects_specific_entry(listing, photos):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
        manufacturer_context="Kia Forte",
    )
    pipeline.run_reconcile(job.pk, "w1")
    finding = job.findings.get(claim_key="c1")
    # Unknown vehicle identity no longer blocks recognition: the
    # general pattern applies to any vehicle, capped at
    # visually_supported. The model's brand guess still never governs
    # applicability.
    assert finding.state == "visually_supported"
    assert "general-acc-distance-control" in finding.summary
    assert "reviewer should confirm" in finding.summary


def test_reconcile_nested_control_supports_claim(listing, photos):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="dashboard button cluster",
        location="left_dashboard",
        controls=[
            {
                "visible_object": "lane keeping assist button",
                "control_symbol": "vehicle between lane lines",
                "location": "left_dashboard",
            }
        ],
    )
    state, summary = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)),
        "lane keeping assist",
        FORTE_2021,
    )
    assert state == "visually_supported"
    assert "control #1" in summary


def test_reconcile_multi_control_no_cross_promotion(listing, photos):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="following-distance button",
        control_symbol="vehicle with distance bars",
        location="steering_wheel",
        dedicated=True,
        controls=[
            {
                "visible_object": "lane keeping assist button",
                "control_symbol": "vehicle between lane lines",
                "location": "left_dashboard",
            },
            {
                "visible_object": "cruise set and resume buttons",
                "location": "steering_wheel",
            },
        ],
    )
    rows = list(Observation.objects.filter(job=job))
    state, summary = pipeline.reconcile_claim(
        rows, "adaptive cruise control", FORTE_2021
    )
    assert state == "visually_supported"
    assert "control #0" in summary
    state, _ = pipeline.reconcile_claim(rows, "lane keeping assist", FORTE_2021)
    assert state == "visually_supported"
    state, _ = pipeline.reconcile_claim(rows, "lane centering", FORTE_2021)
    assert state == "insufficient_evidence"


def test_reconcile_two_supporting_controls(listing, photos):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="following-distance button",
        location="steering_wheel",
        controls=[
            {
                "visible_object": "SCC follow control",
                "control_symbol": "distance bars",
                "location": "steering_wheel",
            }
        ],
    )
    state, _ = pipeline.reconcile_claim(
        list(Observation.objects.filter(job=job)),
        "adaptive cruise control",
        FORTE_2021,
    )
    assert state == "visually_supported"


def test_reconcile_all_control_interpretations_preserved(listing, photos):
    listing.year = 2021
    listing.make = "Kia"
    listing.model = "Forte"
    listing.save(update_fields=["year", "make", "model"])
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="button cluster",
        location="steering_wheel",
        controls=[
            {
                "visible_object": "following-distance button",
                "location": "steering_wheel",
            },
            {
                "visible_object": "cruise set and resume buttons",
                "location": "steering_wheel",
            },
        ],
    )
    pipeline.run_reconcile(job.pk, "w1")
    finding = job.findings.get(claim_key="c1")
    assert finding.state == "visually_supported"
    kept = finding.evidence["interpretation"]["interpretations"]
    assert [entry["control_index"] for entry in kept] == [0, 1, 2]
    assert [entry["supports_claim"] for entry in kept] == [False, True, False]
    assert kept[1]["entry_id"] == "kia-forte-2021-scc-distance"
    assert kept[1]["photo_id"] == str(photos[0].pk)


def test_sam_target_from_observed_object():
    from audits.models import Observation as Obs

    cases = [
        (
            {"visible_object": "following-distance button"},
            "following-distance button",
        ),
        (
            {"visible_object": "button", "control_symbol": ""},
            "button",
        ),
        (
            {
                "visible_object": "lane assistance button",
                "control_symbol": "vehicle between lanes",
            },
            "lane assistance button",
        ),
        (
            {"visible_object": "none", "control_symbol": "steering icon"},
            "steering icon",
        ),
    ]
    for extra, expected in cases:
        concept, source, index = pipeline.sam_target_for(
            Obs(extra=extra), "adaptive cruise control"
        )
        assert concept == expected
        assert source == "observed_object"
        assert index == 0


def test_sam_target_legacy_fallback_and_unavailable():
    from audits.models import Observation as Obs

    concept, source, index = pipeline.sam_target_for(
        Obs(extra={"supports_localization": True}), "adaptive cruise control!"
    )
    assert concept == "adaptive cruise control"
    assert source == "claim_fallback"
    assert index is None
    concept, source, index = pipeline.sam_target_for(
        Obs(extra={"visible_object": "!!!"}), "adaptive cruise control"
    )
    assert concept == ""
    assert source == "unavailable"
    assert index is None


def test_sam_target_selects_nested_claim_relevant_control():
    from audits.models import Observation as Obs

    extra = {
        "visible_object": "cruise set and resume buttons",
        "location": "steering_wheel",
        "controls": [
            {
                "visible_object": "following-distance button",
                "control_symbol": "DISTANCE",
                "location": "steering_wheel",
            }
        ],
    }
    concept, source, index = pipeline.sam_target_for(
        Obs(extra=extra), "adaptive cruise control", FORTE_2021
    )
    assert concept == "following-distance button"
    assert source == "observed_object"
    assert index == 1


def test_sam_target_prefers_supporting_top_level():
    from audits.models import Observation as Obs

    extra = {
        "visible_object": "following-distance button",
        "location": "steering_wheel",
        "dedicated": True,
        "controls": [
            {
                "visible_object": "lane keeping assist button",
                "location": "left_dashboard",
            }
        ],
    }
    concept, source, index = pipeline.sam_target_for(
        Obs(extra=extra), "adaptive cruise control", FORTE_2021
    )
    assert concept == "following-distance button"
    assert source == "observed_object"
    assert index == 0


def test_sam_target_prefers_claim_relevant_over_unrelated():
    from audits.models import Observation as Obs

    extra = {
        "visible_object": "cup holder",
        "controls": [
            {
                "visible_object": "cruise set and resume buttons",
                "location": "steering_wheel",
            }
        ],
    }
    concept, source, index = pipeline.sam_target_for(
        Obs(extra=extra), "cruise control", FORTE_2021
    )
    assert concept == "cruise set and resume buttons"
    assert source == "observed_object"
    assert index == 1


def test_sam_target_skips_none_candidates():
    from audits.models import Observation as Obs

    extra = {
        "visible_object": "none",
        "controls": [{"visible_object": "none"}],
    }
    concept, source, index = pipeline.sam_target_for(
        Obs(extra=extra), "adaptive cruise control", FORTE_2021
    )
    assert concept == ""
    assert source == "unavailable"
    assert index is None


def test_sam_receives_object_not_claim(listing, photos, enabled):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="following-distance button",
        control_symbol="vehicle with distance bars",
        supports_localization=True,
    )
    sam = FakeSamAdapter(records=[_segment()])
    assert pipeline.run_sam(job.pk, "w1", sam) == 1
    assert sam.concepts == ["following-distance button"]
    obs = Observation.objects.get(job=job, source=ObservationSource.SAM)
    assert obs.extra["sam_target"] == "following-distance button"
    assert obs.extra["target_source"] == "observed_object"


def test_sam_localizes_nested_control_and_records_index(listing, photos, enabled):
    listing.year = 2021
    listing.make = "Kia"
    listing.model = "Forte"
    listing.save(update_fields=["year", "make", "model"])
    job = _running_job(listing)
    muse_row = _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="cruise set and resume buttons",
        location="steering_wheel",
        supports_localization=True,
        controls=[
            {
                "visible_object": "following-distance button",
                "control_symbol": "DISTANCE",
                "location": "steering_wheel",
            }
        ],
    )
    sam = FakeSamAdapter(records=[_segment()])
    assert pipeline.run_sam(job.pk, "w1", sam) == 1
    assert sam.calls == 1
    assert sam.concepts == ["following-distance button"]
    obs = Observation.objects.get(job=job, source=ObservationSource.SAM)
    assert obs.extra["control_index"] == 1
    assert obs.extra["source_observation_id"] == str(muse_row.id)


def test_sam_skips_unsanitizable_target_without_spend(listing, photos, enabled):
    job = _running_job(listing)
    _keyed_row(
        job,
        photos[0],
        ObservationSource.Muse,
        Verdict.PRESENT,
        visible_object="!!!",
        supports_localization=True,
    )
    sam = FakeSamAdapter(records=[_segment()])
    assert pipeline.run_sam(job.pk, "w1", sam) == 0
    assert sam.calls == 0
    obs = Observation.objects.get(job=job, source=ObservationSource.SAM)
    assert obs.verdict == Verdict.UNCLEAR


def test_muse_persists_control_fields(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter(
        [
            [
                _obs(
                    str(photos[0].id),
                    "c1",
                    visible_object="following-distance button",
                    control_symbol="vehicle with distance bars",
                    control_type="button",
                    location="steering_wheel",
                    dedicated=True,
                    manufacturer_context="Kia Forte",
                ),
                _obs(str(photos[0].id), "c2", visible_object="none"),
            ],
            [
                _obs(str(photos[1].id), "c1", visible_object="none"),
                _obs(str(photos[1].id), "c2", visible_object="none"),
            ],
        ]
    )
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 4
    obs = Observation.objects.get(
        job=job,
        claim_key="c1",
        photo=photos[0],
        source=ObservationSource.Muse,
    )
    assert obs.extra["control_symbol"] == "vehicle with distance bars"
    assert obs.extra["location"] == "steering_wheel"
    assert obs.extra["dedicated"] is True
    assert obs.extra["manufacturer_context"] == "Kia Forte"


def test_muse_persists_nested_controls(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter(
        [
            [
                _obs(
                    str(photos[0].id),
                    "c1",
                    visible_object="button cluster",
                    controls=[
                        {"visible_object": "following-distance button"},
                        {"visible_object": "cruise set button"},
                    ],
                ),
                _obs(str(photos[0].id), "c2", visible_object="none"),
            ],
            [
                _obs(str(photos[1].id), "c1", visible_object="none"),
                _obs(str(photos[1].id), "c2", visible_object="none"),
            ],
        ]
    )
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 4
    obs = Observation.objects.get(
        job=job,
        claim_key="c1",
        photo=photos[0],
        source=ObservationSource.Muse,
    )
    assert len(obs.extra["controls"]) == 2


def test_obscured_icon_stays_insufficient(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = ScriptedMuseAdapter(
        [
            [
                _obs(
                    str(photos[0].id),
                    "c1",
                    visible_object="partially obscured button, symbol unreadable",
                    ambiguous=True,
                ),
                _obs(str(photos[0].id), "c2", visible_object="none"),
            ],
            [
                _obs(str(photos[1].id), "c1", visible_object="none"),
                _obs(str(photos[1].id), "c2", visible_object="none"),
            ],
        ]
    )
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 4
    done = pipeline.run_reconcile(job.pk, "w1")
    assert done == 2
    finding = job.findings.get(claim_key="c1")
    assert finding.state == "insufficient_evidence"


def test_adas_fixtures_flow_through_pipeline(enabled):
    """Synthetic ADAS schematics drive the real pipeline end to end.

    The PNGs are stick-figure drawings, so scripted (mock) observations
    stand in for the model: this proves bytes, hashing, batching, SAM
    object targets, and catalog mapping compose — not recognition skill.
    """
    import json
    from pathlib import Path

    adas_dir = Path(__file__).resolve().parents[2] / "fixtures" / "adas"
    truth = json.loads((adas_dir / "ground_truth.json").read_text(encoding="utf-8"))
    vehicle = truth["vehicle"]
    listing = Listing.objects.create(
        source_url="https://example.test/l/adas-fixtures",
        source_site="example.test",
        title="adas fixtures",
        year=vehicle["year"],
        make=vehicle["make"],
        model=vehicle["model"],
        trim=vehicle["trim"],
        advertised_claims=truth["claims"],
        expected_photo_count=len(truth["photos"]),
    )
    photos = []
    for entry in truth["photos"]:
        photo, _ = images.store_listing_photo(
            listing,
            data=(adas_dir / entry["file"]).read_bytes(),
            filename=entry["file"],
            source_url=None,
            provenance="extension",
        )
        photos.append(photo)
    scripts = []
    for photo, entry in zip(photos, truth["photos"]):
        batch = []
        for cid in ("c1", "c2"):
            control = entry["observations"][cid]
            if control is None:
                batch.append(_obs(str(photo.id), cid, visible_object="none"))
            else:
                batch.append(
                    _obs(
                        str(photo.id),
                        cid,
                        visible_object=control["visible_object"],
                        control_symbol=control.get("control_symbol", ""),
                        control_type=control.get("control_type", ""),
                        location=control["location"],
                        dedicated=control["dedicated"],
                        manufacturer_context=control.get("manufacturer_context", ""),
                    )
                )
        scripts.append(batch)
    job = _running_job(listing)
    sam = FakeSamAdapter(records=[_segment()])
    done = pipeline.run_stages(job.pk, "w1", ScriptedMuseAdapter(scripts), sam)
    assert done.status == JobStatus.COMPLETE
    acc = job.findings.get(claim_key="c1")
    assert acc.state == "visually_supported"
    assert acc.evidence["interpretation"]["entry_id"] == "kia-forte-2021-scc-distance"
    lka = job.findings.get(claim_key="c2")
    assert lka.state == "visually_supported"
    assert (
        lka.evidence["interpretation"]["entry_id"] == "kia-forte-2019-2024-lka-button"
    )
    assert sam.concepts == [
        "following-distance button",
        "lane keeping assist button",
    ]


# --- PR #3 M2: reference-assisted recognition mode ---------------------

ACC_LKA = ["adaptive cruise control", "lane keeping assist"]


def test_reference_block_features():
    block = pipeline.build_reference_block(ACC_LKA)
    assert "distance" in block
    assert "lane" in block
    assert len(block) <= pipeline.REFERENCE_BLOCK_MAX_CHARS


def test_reference_block_unknown_feature_falls_back_empty():
    assert pipeline.build_reference_block(["sunroof"]) == ""
    assert pipeline.build_reference_block([]) == ""


def test_reference_block_dedupes_same_feature():
    one = pipeline.build_reference_block(["adaptive cruise control"])
    two = pipeline.build_reference_block(["adaptive cruise control", "ACC"])
    assert one != ""
    assert one == two


def test_reference_block_caps_whole_snippets():
    texts = [
        "cruise control",
        "adaptive cruise control",
        "lane keeping assist",
        "lane departure warning",
        "lane centering",
    ]
    block = pipeline.build_reference_block(texts)
    assert len(block) <= pipeline.REFERENCE_BLOCK_MAX_CHARS
    assert block.endswith(".")
    assert "cruise" in block.split("\n\n")[0]


def test_baseline_prompt_unchanged(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    scripts = [
        [_batch_obs(str(photo.id), "c1"), _batch_obs(str(photo.id), "c2")]
        for photo in photos
    ]
    adapter = ScriptedMuseAdapter(scripts)
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 4
    for request in adapter.requests:
        assert request.prompt_version == "m2"
        assert request.system_instructions == pipeline.SYSTEM_INSTRUCTIONS
        assert "RSA" not in request.system_instructions
    rows = AuditExecution.objects.filter(job=job, stage=Stage.Muse)
    assert rows.count() == 2
    for row in rows:
        assert row.usage["prompt_version"] == "m2"
        assert row.usage["reference_version"] is None


def test_reference_mode_appends_guidance(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    scripts = [
        [_batch_obs(str(photo.id), "c1"), _batch_obs(str(photo.id), "c2")]
        for photo in photos
    ]
    adapter = ScriptedMuseAdapter(scripts)
    assert pipeline.run_muse(job.pk, "w1", screened, adapter, reference_mode=True) == 4
    for request in adapter.requests:
        assert request.prompt_version == "m3"
        assert request.system_instructions.startswith(pipeline.SYSTEM_INSTRUCTIONS)
        assert "2026-09-rsa-v1" in request.system_instructions
        assert "Report only visible evidence" in request.system_instructions
    rows = AuditExecution.objects.filter(job=job, stage=Stage.Muse)
    assert rows.count() == 2
    for row in rows:
        assert row.usage["prompt_version"] == "m3"
        assert row.usage["reference_version"] == "2026-09-rsa-v1"


# --- Prompt m4: cross-brand pattern guidance (Part B, default OFF) --------

# Exact expected m4 paragraph, written from the task language before the
# implementation: two named examples explicitly marked as non-exhaustive,
# describe-what-is-visible instruction, unchanged location enum + schema.
EXPECTED_PATTERN_PARAGRAPH = (
    "Cross-manufacturer pattern guidance (general knowledge only, never "
    "proof of equipment): equivalent ADAS controls sit in different "
    "places across brands. For example, the Kia Forte lane-keeping "
    "assist button sits on the left dashboard (lower left), while "
    "Jetta/GLI assistance functions sit behind a stalk-end button that "
    "opens an assistance menu. These are TWO EXAMPLES, not a complete "
    "list of brands or locations. Describe exactly what is visible -- "
    "symbol, control type, and location -- even when the brand or "
    "location is not listed here. Location must be one of: "
    "steering_wheel, left_dashboard, stalk, instrument_cluster, "
    "infotainment, center_console, exterior_sensor, windshield_camera, "
    "unknown. The JSON observation schema is unchanged."
)


def test_pattern_guidance_off_by_default(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    scripts = [
        [_batch_obs(str(photo.id), "c1"), _batch_obs(str(photo.id), "c2")]
        for photo in photos
    ]
    adapter = ScriptedMuseAdapter(scripts)
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 4
    for request in adapter.requests:
        assert request.prompt_version == "m2"
        assert request.system_instructions == pipeline.SYSTEM_INSTRUCTIONS
    rows = AuditExecution.objects.filter(job=job, stage=Stage.Muse)
    for row in rows:
        assert row.usage["prompt_version"] == "m2"


def test_pattern_guidance_on_sends_m4(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    scripts = [
        [_batch_obs(str(photo.id), "c1"), _batch_obs(str(photo.id), "c2")]
        for photo in photos
    ]
    adapter = ScriptedMuseAdapter(scripts)
    assert (
        pipeline.run_muse(job.pk, "w1", screened, adapter, pattern_guidance=True) == 4
    )
    expected = pipeline.SYSTEM_INSTRUCTIONS + "\n\n" + EXPECTED_PATTERN_PARAGRAPH
    for request in adapter.requests:
        assert request.prompt_version == "m4"
        assert request.prompt_version == pipeline.PATTERN_PROMPT_VERSION
        assert request.system_instructions == expected
    rows = AuditExecution.objects.filter(job=job, stage=Stage.Muse)
    assert rows.count() == 2
    for row in rows:
        assert row.usage["prompt_version"] == "m4"
        assert row.usage["reference_version"] is None


def test_pattern_guidance_text_exact():
    text = pipeline.build_system_instructions("", pattern_guidance=True)
    assert text == pipeline.SYSTEM_INSTRUCTIONS + "\n\n" + (EXPECTED_PATTERN_PARAGRAPH)
    lowered = text.lower()
    assert "kia forte" in lowered
    assert "stalk" in lowered
    assert "two examples" in lowered
    assert "not a complete list" in lowered
    # Location enum and JSON schema pass through unchanged.
    assert "steering_wheel, left_dashboard, stalk" in text
    assert "'observations' array" in text


def test_reference_mode_unaffected_by_pattern_gate(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    scripts = [
        [_batch_obs(str(photo.id), "c1"), _batch_obs(str(photo.id), "c2")]
        for photo in photos
    ]
    adapter = ScriptedMuseAdapter(scripts)
    assert (
        pipeline.run_muse(
            job.pk,
            "w1",
            screened,
            adapter,
            reference_mode=True,
            pattern_guidance=False,
        )
        == 4
    )
    for request in adapter.requests:
        assert request.prompt_version == "m3"
        assert request.system_instructions.startswith(pipeline.SYSTEM_INSTRUCTIONS)
    rows = AuditExecution.objects.filter(job=job, stage=Stage.Muse)
    assert rows.count() == 2
    for row in rows:
        assert row.usage["prompt_version"] == "m3"
        assert row.usage["reference_version"] is not None


def test_reference_mode_falls_back_without_mapping(listing, photos, enabled):
    listing.advertised_claims = ["sunroof"]
    listing.save(update_fields=["advertised_claims"])
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    scripts = [[_batch_obs(str(photo.id), "c1")] for photo in photos]
    adapter = ScriptedMuseAdapter(scripts)
    assert pipeline.run_muse(job.pk, "w1", screened, adapter, reference_mode=True) == 2
    for request in adapter.requests:
        assert request.prompt_version == "m2"
        assert request.system_instructions == pipeline.SYSTEM_INSTRUCTIONS


# --- Item 5b: reference + pattern combination is unsupported ------------


def test_run_muse_rejects_combined_reference_and_pattern(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    scripts = [
        [_batch_obs(str(photo.id), "c1"), _batch_obs(str(photo.id), "c2")]
        for photo in photos
    ]
    adapter = ScriptedMuseAdapter(scripts)
    with pytest.raises(ValueError, match="mutually exclusive"):
        pipeline.run_muse(
            job.pk,
            "w1",
            screened,
            adapter,
            reference_mode=True,
            pattern_guidance=True,
        )
    assert adapter.calls == 0
