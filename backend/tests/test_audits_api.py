"""Mother audit API: queue, history, detail, findings, evidence, cancel, retry."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from audits import worker
from audits.models import (
    AuditCrop,
    AuditExecution,
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
from listings.models import Listing

from .conftest import post_json

pytestmark = pytest.mark.django_db


@pytest.fixture()
def enabled(settings):
    settings.AUDIT_INFERENCE_ENABLED = True


@pytest.fixture()
def listing():
    return Listing.objects.create(
        source_url="https://example.test/l/api-audit",
        source_site="example.test",
        title="api audit listing",
        advertised_claims=["adaptive cruise control"],
        expected_photo_count=1,
    )


@pytest.fixture()
def no_spawn(monkeypatch):
    """Queue endpoints must not launch real inference threads in tests."""
    calls: list = []
    monkeypatch.setattr(
        worker, "spawn_worker", lambda *a, **k: calls.append((a, k)) or "w-test"
    )
    return calls


def _queue(operator_client, listing_id):
    return post_json(operator_client, f"/api/listings/{listing_id}/audits", {})


def test_queue_creates_job_and_spawns(operator_client, listing, enabled, no_spawn):
    response = _queue(operator_client, listing.pk)
    assert response.status_code == 202, response.content
    body = response.json()
    assert body["created"] is True
    assert body["job"]["status"] == "queued"
    assert body["job"]["listing_id"] == str(listing.pk)
    assert Decimal(body["job"]["spend_cap_usd"]) == Decimal("2.00")
    assert len(no_spawn) == 1


def test_queue_reuses_in_flight_without_respawn(
    operator_client, listing, enabled, no_spawn
):
    first = _queue(operator_client, listing.pk).json()
    second = _queue(operator_client, listing.pk)
    assert second.status_code == 202
    body = second.json()
    assert body["created"] is False
    assert body["job"]["id"] == first["job"]["id"]
    assert len(no_spawn) == 1
    assert AuditJob.objects.count() == 1


def test_queue_refuses_when_disabled(operator_client, listing, settings, no_spawn):
    settings.AUDIT_INFERENCE_ENABLED = False
    response = _queue(operator_client, listing.pk)
    assert response.status_code == 403
    assert len(no_spawn) == 0


def test_queue_unknown_listing(operator_client, enabled, no_spawn):
    response = _queue(operator_client, uuid.uuid4())
    assert response.status_code == 404


def test_queue_requires_operator_session(client, listing, enabled):
    assert client.get(f"/api/listings/{listing.pk}/audits").status_code == 401


def test_queue_requires_csrf(operator_client, listing, enabled):
    import json

    response = operator_client.post(
        f"/api/listings/{listing.pk}/audits",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert response.status_code == 403


def test_list_audits_history(operator_client, listing, enabled, no_spawn):
    _queue(operator_client, listing.pk)
    job = AuditJob.objects.get()
    job.status = JobStatus.COMPLETE
    job.save()
    _queue(operator_client, listing.pk)
    response = operator_client.get(f"/api/listings/{listing.pk}/audits")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0]["status"] == "queued"


def test_get_audit_detail_counts(operator_client, listing, enabled, no_spawn):
    from evidence import images

    from .conftest import make_image_bytes

    photo, _ = images.store_listing_photo(
        listing,
        data=make_image_bytes("PNG", "D1"),
        filename="d1.png",
        source_url=None,
        provenance="extension",
    )
    job_id = _queue(operator_client, listing.pk).json()["job"]["id"]
    job = AuditJob.objects.get(pk=job_id)
    Observation.objects.create(
        job=job,
        claim_key="adaptive cruise control",
        photo=photo,
        source=ObservationSource.Muse,
        verdict=Verdict.PRESENT,
    )
    response = operator_client.get(f"/api/audits/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["observation_count"] == 1
    assert body["current_stage"] == "queued"


def test_get_audit_unknown(operator_client):
    assert operator_client.get(f"/api/audits/{uuid.uuid4()}").status_code == 404


def test_findings_list(operator_client, listing, enabled, no_spawn):
    job_id = _queue(operator_client, listing.pk).json()["job"]["id"]
    job = AuditJob.objects.get(pk=job_id)
    assert operator_client.get(f"/api/audits/{job_id}/findings").json() == []
    AuditFinding.objects.create(
        job=job,
        claim_key="adaptive cruise control",
        state=EvidenceState.CONTRADICTED,
        summary="absent everywhere",
    )
    body = operator_client.get(f"/api/audits/{job_id}/findings").json()
    assert len(body) == 1
    assert body[0]["state"] == "contradicted"


def test_evidence_bundle(operator_client, listing, enabled, no_spawn):
    from evidence import images

    from .conftest import make_image_bytes

    photo, _ = images.store_listing_photo(
        listing,
        data=make_image_bytes("PNG", "E1"),
        filename="e1.png",
        source_url=None,
        provenance="extension",
    )
    job_id = _queue(operator_client, listing.pk).json()["job"]["id"]
    job = AuditJob.objects.get(pk=job_id)
    Observation.objects.create(
        job=job,
        claim_key="adaptive cruise control",
        photo=photo,
        source=ObservationSource.Muse,
        verdict=Verdict.ABSENT,
    )
    AuditExecution.objects.create(
        job=job, stage=Stage.SCREENING, provider=Provider.LOCAL
    )
    body = operator_client.get(f"/api/audits/{job_id}/evidence").json()
    assert len(body["observations"]) == 1
    assert body["observations"][0]["verdict"] == "absent"
    assert body["crops"] == []
    assert len(body["executions"]) == 1
    assert body["executions"][0]["cost_usd"] is None


def test_download_crop_roundtrip(operator_client, listing, enabled, no_spawn):
    from evidence import images

    from .conftest import make_image_bytes

    photo, _ = images.store_listing_photo(
        listing,
        data=make_image_bytes("PNG", "C1"),
        filename="c1.png",
        source_url=None,
        provenance="extension",
    )
    job_id = _queue(operator_client, listing.pk).json()["job"]["id"]
    job = AuditJob.objects.get(pk=job_id)
    from PIL import Image

    from audits import pipeline

    ref = "a" * 32
    dest = pipeline.audit_dirs(job.pk)["crops"] / f"{ref}.png"
    Image.new("RGB", (8, 8)).save(dest, format="PNG")
    crop = AuditCrop.objects.create(
        job=job,
        source_photo=photo,
        box=[0, 0, 8, 8],
        storage_ref=ref,
        width=8,
        height=8,
    )
    response = operator_client.get(f"/api/audits/{job_id}/crops/{crop.pk}")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert len(b"".join(response.streaming_content)) > 0
    assert (
        operator_client.get(f"/api/audits/{job_id}/crops/{uuid.uuid4()}").status_code
        == 404
    )
    missing = AuditCrop.objects.create(
        job=job,
        source_photo=photo,
        box=[0, 0, 8, 8],
        storage_ref="c" * 32,
        width=8,
        height=8,
    )
    assert (
        operator_client.get(f"/api/audits/{job_id}/crops/{missing.pk}").status_code
        == 410
    )


def test_download_mask_roundtrip(operator_client, listing, enabled, no_spawn):
    job_id = _queue(operator_client, listing.pk).json()["job"]["id"]
    job = AuditJob.objects.get(pk=job_id)
    from audits import pipeline

    ref = pipeline.write_mask_png(job.pk, bytes(16 * 16), 16, 16)
    response = operator_client.get(f"/api/audits/{job_id}/masks/{ref}")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert (
        operator_client.get(f"/api/audits/{job_id}/masks/not-a-ref").status_code == 404
    )
    assert (
        operator_client.get(f"/api/audits/{job_id}/masks/{'b' * 32}").status_code == 410
    )


def test_cancel_queued_job(operator_client, listing, enabled, no_spawn):
    job_id = _queue(operator_client, listing.pk).json()["job"]["id"]
    response = post_json(operator_client, f"/api/audits/{job_id}/cancel", {})
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_cancel_terminal_is_conflict(operator_client, listing, enabled, no_spawn):
    job_id = _queue(operator_client, listing.pk).json()["job"]["id"]
    post_json(operator_client, f"/api/audits/{job_id}/cancel", {})
    response = post_json(operator_client, f"/api/audits/{job_id}/cancel", {})
    assert response.status_code == 409


def test_retry_terminal_starts_fresh(operator_client, listing, enabled, no_spawn):
    job_id = _queue(operator_client, listing.pk).json()["job"]["id"]
    post_json(operator_client, f"/api/audits/{job_id}/cancel", {})
    response = post_json(operator_client, f"/api/audits/{job_id}/retry", {})
    assert response.status_code == 202, response.content
    body = response.json()
    assert body["created"] is True
    assert body["job"]["id"] != job_id


def test_retry_in_flight_is_conflict(operator_client, listing, enabled, no_spawn):
    job_id = _queue(operator_client, listing.pk).json()["job"]["id"]
    response = post_json(operator_client, f"/api/audits/{job_id}/retry", {})
    assert response.status_code == 409


def test_retry_paused_starts_fresh(operator_client, listing, enabled, no_spawn):
    job_id = _queue(operator_client, listing.pk).json()["job"]["id"]
    AuditJob.objects.filter(pk=job_id).update(
        status=JobStatus.PAUSED,
        error_code="spend_cap",
        error_detail="reached cap",
    )
    response = post_json(operator_client, f"/api/audits/{job_id}/retry", {})
    assert response.status_code == 202, response.content
    body = response.json()
    assert body["created"] is True
    assert body["job"]["id"] != job_id


def test_queue_after_paused_starts_fresh(operator_client, listing, enabled, no_spawn):
    job_id = _queue(operator_client, listing.pk).json()["job"]["id"]
    AuditJob.objects.filter(pk=job_id).update(status=JobStatus.PAUSED)
    body = _queue(operator_client, listing.pk).json()
    assert body["created"] is True
    assert body["job"]["id"] != job_id


# --- worker --------------------------------------------------------------


def test_worker_runs_claimed_job_to_complete(listing, enabled):
    from evidence import images

    from .conftest import make_image_bytes
    from .test_audits import FakeMuseAdapter, FakeSamAdapter, _obs, _segment

    photo, _ = images.store_listing_photo(
        listing,
        data=make_image_bytes("PNG", "W1"),
        filename="w1.png",
        source_url=None,
        provenance="extension",
    )
    from audits import services

    services.queue_audit(listing.pk)
    muse = FakeMuseAdapter(observations=[_obs(str(photo.id), "c1")])
    sam = FakeSamAdapter(records=[_segment(size=(64, 48))])
    settled = worker.run_claimed_job("w-test", muse, sam)
    assert settled is not None
    assert settled.status == JobStatus.COMPLETE
    assert settled.findings.count() == 1
    assert settled.spend_usd > Decimal("0.0000")


def test_worker_no_job_returns_none():
    assert worker.run_claimed_job("w-idle") is None


def test_worker_crash_fails_job(listing, enabled):
    from audits import services
    from evidence import images

    from .conftest import make_image_bytes

    images.store_listing_photo(
        listing,
        data=make_image_bytes("PNG", "W2"),
        filename="w2.png",
        source_url=None,
        provenance="extension",
    )
    services.queue_audit(listing.pk)

    class Boom:
        model_id = "boom"

        def analyze(self, request):
            raise ValueError("kaboom")

    settled = worker.run_claimed_job("w-test", Boom(), Boom())
    assert settled is not None
    assert settled.status == JobStatus.FAILED
    assert settled.error_code == "worker_crash"


def test_extension_token_rejected_on_audit_api(client, ext_headers, listing, enabled):
    response = client.get(f"/api/listings/{listing.pk}/audits", headers=ext_headers)
    assert response.status_code == 401
