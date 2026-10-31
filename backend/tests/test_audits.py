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
from providers.base import ProviderRateLimitError, UsageRecord
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
        job.pk, "w1", stage=Stage.SCREENING, provider=Provider.LOCAL
    )
    job.refresh_from_db()
    assert job.calls_made == 1
    assert job.spend_usd == Decimal("0.0000")
    assert job.unknown_cost_calls == 0
    assert job.status == JobStatus.RUNNING


def test_record_known_cost_accumulates(listing):
    job = _running_job(listing)
    services.record_execution(
        job.pk,
        "w1",
        stage=Stage.Muse,
        provider=Provider.BASETEN,
        cost_usd=Decimal("0.0201"),
    )
    job.refresh_from_db()
    assert job.spend_usd == Decimal("0.0201")
    assert job.status == JobStatus.RUNNING


def test_unknown_cost_pauses_job(listing):
    job = _running_job(listing)
    with pytest.raises(services.BudgetExceededError):
        services.record_execution(
            job.pk,
            "w1",
            stage=Stage.Muse,
            provider=Provider.BASETEN,
            cost_usd=None,
        )
    job.refresh_from_db()
    assert job.status == JobStatus.PAUSED
    assert job.error_code == "unknown_cost"
    assert job.unknown_cost_calls == 1
    # Spend never absorbs the unknown call as zero.
    assert job.spend_usd == Decimal("0.0000")


def test_spend_cap_pauses_job(listing):
    job = _running_job(listing, spend_cap_usd=Decimal("0.05"))
    services.record_execution(
        job.pk,
        "w1",
        stage=Stage.Muse,
        provider=Provider.BASETEN,
        cost_usd=Decimal("0.06"),
    )
    job.refresh_from_db()
    assert job.status == JobStatus.PAUSED
    assert job.error_code == "spend_cap"


def test_call_cap_blocks_next_call(listing):
    job = _running_job(listing, max_calls=1)
    services.record_execution(
        job.pk, "w1", stage=Stage.SCREENING, provider=Provider.LOCAL
    )
    with pytest.raises(services.BudgetExceededError):
        services.check_budget(AuditJob.objects.get(pk=job.pk), "w1")
    job.refresh_from_db()
    assert job.status == JobStatus.PAUSED
    assert job.error_code == "call_cap"


def test_stale_worker_cannot_write(listing):
    job = _running_job(listing)
    with pytest.raises(services.StaleWorkerError):
        services.record_execution(
            job.pk, "intruder", stage=Stage.SCREENING, provider=Provider.LOCAL
        )
    assert AuditExecution.objects.count() == 0
    with pytest.raises(services.StaleWorkerError):
        services.complete_job(job.pk, "intruder")


def test_paused_job_rejects_writes(listing):
    job = _running_job(listing, status=JobStatus.PAUSED)
    with pytest.raises(services.JobNotRunningError):
        services.record_execution(
            job.pk, "w1", stage=Stage.SCREENING, provider=Provider.LOCAL
        )


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
    adapter = FakeMuseAdapter(
        observations=[
            _obs(str(photos[0].id), "adaptive cruise control"),
            _obs(
                str(photos[1].id),
                "adaptive cruise control",
                visible_object="none",
            ),
            _obs(str(photos[0].id), "lane keeping assist", ambiguous=True),
        ]
    )
    stored = pipeline.run_muse(job.pk, "w1", screened, adapter)
    assert stored == 3
    rows = {
        (o.claim_key, str(o.photo_id)): o.verdict
        for o in Observation.objects.filter(job=job)
    }
    assert rows[("adaptive cruise control", str(photos[0].id))] == Verdict.PRESENT
    assert rows[("adaptive cruise control", str(photos[1].id))] == Verdict.ABSENT
    assert rows[("lane keeping assist", str(photos[0].id))] == Verdict.UNCLEAR
    job.refresh_from_db()
    assert job.spend_usd == Decimal("0.0123")


def test_muse_rerun_is_idempotent(listing, photos, enabled):
    job = _running_job(listing)
    screened = pipeline.run_screening(job.pk, "w1")
    adapter = FakeMuseAdapter(
        observations=[_obs(str(photos[0].id), "adaptive cruise control")]
    )
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 1
    assert pipeline.run_muse(job.pk, "w1", screened, adapter) == 0
    assert Observation.objects.filter(job=job).count() == 1


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
    adapter = FakeMuseAdapter(
        observations=[_obs(str(photos[0].id), "adaptive cruise control")],
        cost=None,
    )
    with pytest.raises(services.BudgetExceededError):
        pipeline.run_muse(job.pk, "w1", screened, adapter)
    job.refresh_from_db()
    assert job.status == JobStatus.PAUSED
    assert job.error_code == "unknown_cost"
    assert Observation.objects.filter(job=job).count() == 0


def test_run_stages_rejects_stale_worker(listing, photos, enabled):
    job = _running_job(listing)
    with pytest.raises(services.StaleWorkerError):
        pipeline.run_stages(job.pk, "intruder")


def test_current_stage_defaults(listing):
    queued = AuditJob.objects.create(listing=listing)
    assert queued.current_stage == JobStatus.QUEUED
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


def _present_muse_obs(job, photo, claim="adaptive cruise control", **extra):
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


def test_sam_unlocalizable_claim_is_unclear(listing, photos, enabled):
    job = _running_job(listing)
    _present_muse_obs(job, photos[0], claim="!?.")
    adapter = FakeSamAdapter(records=[_segment()])
    assert pipeline.run_sam(job.pk, "w1", adapter) == 0
    assert adapter.calls == 0
    obs = Observation.objects.get(job=job, source=ObservationSource.SAM)
    assert obs.verdict == Verdict.UNCLEAR


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


def _crop_row(job, photo):
    from audits.models import AuditCrop

    ref = "cropref1"
    dest = pipeline.audit_dirs(job.pk)["crops"] / f"{ref}.png"
    from PIL import Image

    Image.new("RGB", (16, 16)).save(dest, format="PNG")
    return AuditCrop.objects.create(
        job=job,
        source_photo=photo,
        box=[0, 0, 16, 16],
        storage_ref=ref,
        width=16,
        height=16,
    )


def test_recheck_confirms_crop(listing, photos, enabled):
    job = _running_job(listing)
    _crop_row(job, photos[0])
    adapter = FakeMuseAdapter(
        observations=[
            _obs(str(photos[0].id), "adaptive cruise control"),
            _obs(
                str(photos[0].id),
                "lane keeping assist",
                visible_object="none",
            ),
        ]
    )
    assert pipeline.run_recheck(job.pk, "w1", adapter) == 1
    rows = {
        o.claim_key: o.verdict
        for o in Observation.objects.filter(job=job, source=ObservationSource.RECHECK)
    }
    assert rows == {
        "adaptive cruise control": Verdict.PRESENT,
        "lane keeping assist": Verdict.ABSENT,
    }
    row = AuditExecution.objects.get(job=job, stage=Stage.RECHECK)
    assert row.usage["prompt_version"] == "m1-recheck"


def test_recheck_skips_covered_crops(listing, photos, enabled):
    job = _running_job(listing)
    crop = _crop_row(job, photos[0])
    for claim in ("adaptive cruise control", "lane keeping assist"):
        Observation.objects.create(
            job=job,
            claim_key=claim,
            photo=photos[0],
            source=ObservationSource.RECHECK,
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
    _obs_row(job, photos[0], ObservationSource.Muse, Verdict.ABSENT)
    _obs_row(job, photos[1], ObservationSource.Muse, Verdict.ABSENT)
    assert pipeline.run_reconcile(job.pk, "w1") == 2
    findings = {f.claim_key: f.state for f in AuditFinding.objects.filter(job=job)}
    assert findings == {
        "adaptive cruise control": "insufficient_evidence",
        "lane keeping assist": "insufficient_evidence",
    }
    assert (
        AuditFinding.objects.get(job=job, claim_key="adaptive cruise control").evidence[
            "rule"
        ]
        == "v1"
    )
    job.refresh_from_db()
    assert job.status == JobStatus.COMPLETE


def test_full_pipeline_completes_with_findings(listing, photos, enabled):
    from audits.models import AuditFinding

    job = _running_job(listing)
    muse = FakeMuseAdapter(
        observations=[
            _obs(str(photos[0].id), "adaptive cruise control"),
            _obs(
                str(photos[1].id),
                "adaptive cruise control",
                visible_object="none",
            ),
            _obs(str(photos[0].id), "lane keeping assist", visible_object="none"),
            _obs(str(photos[1].id), "lane keeping assist", visible_object="none"),
        ]
    )
    sam = FakeSamAdapter(records=[_segment()])
    done = pipeline.run_stages(job.pk, "w1", muse, sam)
    assert done.status == JobStatus.COMPLETE
    findings = {f.claim_key: f.state for f in AuditFinding.objects.filter(job=job)}
    assert findings == {
        "adaptive cruise control": "visually_supported",
        "lane keeping assist": "insufficient_evidence",
    }
    stages = list(
        AuditExecution.objects.filter(job=job)
        .order_by("created_at")
        .values_list("stage", flat=True)
    )
    assert stages == [
        "screening",
        "muse",
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
