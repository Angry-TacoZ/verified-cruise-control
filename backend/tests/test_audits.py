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
            prompt_version=pipeline.PROMPT_VERSION,
            usage=UsageRecord(
                provider="baseten",
                model_id=self.model_id,
                prompt_version=pipeline.PROMPT_VERSION,
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
        "visible_object": True,
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
                visible_object=False,
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


def test_run_stages_leaves_job_running_for_later_stages(listing, photos, enabled):
    job = _running_job(listing)
    adapter = FakeMuseAdapter(
        observations=[_obs(str(photos[0].id), "adaptive cruise control")]
    )
    done = pipeline.run_stages(job.pk, "w1", adapter)
    assert done.status == JobStatus.RUNNING
    assert done.current_stage == Stage.Muse
    assert Observation.objects.filter(job=job).count() == 1
    assert AuditExecution.objects.filter(job=job).count() == 2


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
