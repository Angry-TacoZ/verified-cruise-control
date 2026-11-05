"""Photo triage: one cheap call narrows full observation to relevant photos.

Triage only ever saves calls: any triage failure falls back to the full
photo set, and excluded photos are ledger-recorded, never silent.
"""

# ruff: noqa: F811 -- listing/photos/enabled are re-exported from
# test_audits so this module shares one fixture definition; each test
# function legitimately shadows them as fixture parameters.

from __future__ import annotations

import io

import pytest
from PIL import Image

from audits import pipeline
from audits.models import AuditExecution, JobStatus, Observation, Provider, Stage
from evidence import images
from listings.models import Listing
from providers.base import ProviderResponseError
from providers.meta_muse import validate_triage_payload

from .conftest import make_image_bytes
from .test_audits import (
    FakeMuseAdapter,
    FakeSamAdapter,
    _obs,
    _running_job,
    _segment,
    enabled,  # noqa: F401  (shared fixture)
    listing,  # noqa: F401  (shared fixture)
    photos,  # noqa: F401  (shared fixture)
)

pytestmark = pytest.mark.django_db


def _screened(job_pk):
    return pipeline.run_screening(job_pk, "w1")


def test_validate_triage_accepts_exact_coverage():
    included, warnings = validate_triage_payload(
        {"relevant": {"a": True, "b": False}}, ["a", "b"]
    )
    assert included == ["a"]
    assert warnings == []


def test_validate_triage_rejects_partial_coverage():
    with pytest.raises(ProviderResponseError, match="1 of 2 photos"):
        validate_triage_payload({"relevant": {"a": True}}, ["a", "b"])


def test_validate_triage_rejects_unknown_ids():
    with pytest.raises(ProviderResponseError, match="2 of 1 photos"):
        validate_triage_payload({"relevant": {"a": True, "zzz": True}}, ["a"])


def test_validate_triage_rejects_non_boolean():
    with pytest.raises(ProviderResponseError, match="non-boolean"):
        validate_triage_payload({"relevant": {"a": "yes"}}, ["a"])


def test_validate_triage_rejects_non_object():
    with pytest.raises(ProviderResponseError, match="not a JSON object"):
        validate_triage_payload([1, 2], ["a"])


def test_triage_prompt_names_only_wheel_and_dash():
    text = pipeline.build_triage_text(["Adaptive Cruise Control"], ["p1"])
    assert "steering wheel" in text
    assert "dashboard" in text
    system = pipeline.TRIAGE_SYSTEM_INSTRUCTIONS
    assert "TWO places" in system
    assert "radar housing alone never proves ACC" in system


def test_thumbnails_are_small_valid_jpegs(listing, photos, enabled):
    job = _running_job(listing)
    thumbs = pipeline.build_triage_thumbnails(job.pk, "w1", photos)
    assert len(thumbs) == 2
    for thumb in thumbs:
        assert thumb.mime_type == "image/jpeg"
        with Image.open(io.BytesIO(thumb.data)) as img:
            assert max(img.size) <= pipeline.TRIAGE_THUMBNAIL_MAX_PX


def test_triage_narrows_to_relevant_photos(listing, photos, enabled):
    job = _running_job(listing)
    screened = _screened(job.pk)
    adapter = FakeMuseAdapter(triage_result=[str(photos[0].id)])
    subset = pipeline.run_triage(job.pk, "w1", screened, adapter)
    assert [p.pk for p in subset] == [photos[0].pk]
    assert adapter.triage_calls == 1
    row = AuditExecution.objects.get(job=job, usage__prompt_version="triage-v1")
    assert row.provider == Provider.META
    assert row.ok is True
    assert row.usage["triage_included"] == 1
    assert row.usage["triage_excluded"] == [str(photos[1].id)]


def test_triage_error_falls_back_to_full_set(listing, photos, enabled):
    from providers.base import ProviderTimeoutError

    job = _running_job(listing)
    screened = _screened(job.pk)
    adapter = FakeMuseAdapter(triage_error=ProviderTimeoutError("hung"))
    subset = pipeline.run_triage(job.pk, "w1", screened, adapter)
    assert [p.pk for p in subset] == [p.pk for p in photos]
    job.refresh_from_db()
    assert job.status == JobStatus.RUNNING
    row = AuditExecution.objects.get(
        job=job, stage=Stage.Muse, usage__triage="fallback-full-set"
    )
    assert row.ok is False


def test_triage_garbage_falls_back_to_full_set(listing, photos, enabled):
    job = _running_job(listing)
    screened = _screened(job.pk)
    adapter = FakeMuseAdapter(triage_result=[str(photos[0].id), "nope"])
    subset = pipeline.run_triage(job.pk, "w1", screened, adapter)
    assert [p.pk for p in subset] == [p.pk for p in photos]
    row = AuditExecution.objects.get(
        job=job, stage=Stage.Muse, usage__triage="fallback-full-set"
    )
    assert row.usage["reason"] == "unknown-photo-ids"


def test_full_observation_skips_excluded_photos(listing, photos, enabled):
    job = _running_job(listing)
    screened = _screened(job.pk)
    triage = FakeMuseAdapter(triage_result=[str(photos[0].id)])
    subset = pipeline.run_triage(job.pk, "w1", screened, triage)
    screened_subset = pipeline.ScreenedJob(claims=screened.claims, photos=subset)
    muse = FakeMuseAdapter(
        observations=[
            _obs(str(photos[0].id), "c1"),
            _obs(str(photos[0].id), "c2"),
        ]
    )
    stored = pipeline.run_muse(job.pk, "w1", screened_subset, muse)
    assert stored == 2
    assert muse.calls == 1
    assert triage.triage_calls == 1
    assert Observation.objects.filter(job=job).count() == 2
    job.refresh_from_db()
    assert job.status == JobStatus.RUNNING


def _photo_with_name(listing, filename):
    photo, _ = images.store_listing_photo(
        listing,
        data=make_image_bytes("PNG", filename),
        filename=filename,
        source_url=None,
        provenance="extension",
    )
    return photo


def _mini_listing():
    return Listing.objects.create(
        source_url="https://example.test/l/mini",
        source_site="example.test",
        title="2024 MINI Cooper",
        year=2024,
        make="MINI",
        model="Cooper",
        advertised_claims=["adaptive cruise control"],
        expected_photo_count=3,
    )


def test_filename_filter_excludes_wrong_vehicle_before_triage(enabled):
    listing = _mini_listing()
    keep = _photo_with_name(listing, "2024_mini_cooper-pic-a.jpg")
    ambiguous = _photo_with_name(listing, "interior.jpg")
    wrong = _photo_with_name(listing, "2020_dodge_challenger-pic-b.jpg")
    job = _running_job(listing)
    screened = _screened(job.pk)
    adapter = FakeMuseAdapter(triage_result=[str(keep.pk), str(ambiguous.pk)])
    subset = pipeline.run_triage(job.pk, "w1", screened, adapter)
    assert [p.pk for p in subset] == [keep.pk, ambiguous.pk]
    # The triage call itself never saw the wrong-vehicle photo.
    assert adapter.triage_calls == 1
    row = AuditExecution.objects.get(job=job, stage=Stage.Muse, provider=Provider.LOCAL)
    assert row.usage["prompt_version"] == "triage-v1"
    assert row.usage["filename_excluded"] == [str(wrong.pk)]
    triage_row = AuditExecution.objects.get(
        job=job,
        stage=Stage.Muse,
        provider=Provider.META,
        usage__prompt_version="triage-v1",
        ok=True,
    )
    assert triage_row.usage["filename_excluded"] == [str(wrong.pk)]


def test_filename_filter_fails_open_when_all_excluded(enabled):
    listing = _mini_listing()
    first = _photo_with_name(listing, "2020_dodge_challenger-pic-a.jpg")
    second = _photo_with_name(listing, "2023_audi_q5-pic-b.jpg")
    job = _running_job(listing)
    screened = _screened(job.pk)
    assert {str(first.pk), str(second.pk)} == {str(p.pk) for p in screened.photos}
    adapter = FakeMuseAdapter(triage_result=[str(first.pk)])
    subset = pipeline.run_triage(job.pk, "w1", screened, adapter)
    assert [p.pk for p in subset] == [first.pk]
    assert (
        AuditExecution.objects.filter(
            job=job, stage=Stage.Muse, provider=Provider.LOCAL
        ).count()
        == 0
    )


def test_filename_filter_skipped_without_listing_vehicle(listing, photos, enabled):
    dodge = _photo_with_name(listing, "2020_dodge_challenger-pic-a.jpg")
    job = _running_job(listing)
    screened = _screened(job.pk)
    ids = [str(p.pk) for p in screened.photos]
    assert str(dodge.pk) in ids
    adapter = FakeMuseAdapter(triage_result=ids)
    subset = pipeline.run_triage(job.pk, "w1", screened, adapter)
    assert [p.pk for p in subset] == [p.pk for p in screened.photos]


def test_stages_wire_triage_before_muse(listing, photos, enabled):
    job = _running_job(listing)
    muse = FakeMuseAdapter(
        observations=[
            _obs(str(photos[0].id), "c1"),
            _obs(str(photos[0].id), "c2"),
        ],
        triage_result=[str(photos[0].id)],
    )
    done = pipeline.run_stages(job.pk, "w1", muse, FakeSamAdapter(records=[_segment()]))
    assert done.status == JobStatus.COMPLETE
    assert muse.triage_calls == 1
    assert muse.calls == 1
