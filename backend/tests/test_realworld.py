"""Unit tests for the M3/M4 real-world evaluation scaffold.

Covers manifest validation, deterministic scoring, and spend estimation.
All tests are offline; no provider calls are made.
"""

from pathlib import Path

import pytest

from audits.realworld import (
    MANIFEST_VERSION,
    estimate_spend,
    render_markdown,
    score_case,
    summarize,
    validate_case,
    validate_manifest,
)


def _case(**overrides):
    case = {
        "id": "test-001",
        "vehicle": {
            "year": 2021,
            "make": "Kia",
            "model": "Forte",
            "trim": "GT-Line",
            "market": "US",
        },
        "claim": {
            "advertised": "Lane Keeping Assist",
            "normalized_feature": "lane_keeping_assist",
            "claim_id": "lka",
        },
        "photo": {
            "path": "photos/test-001.jpg",
            "sha256": "a" * 64,
            "provenance": "photographed by evaluator",
        },
        "ground_truth": {
            "equipment": "equipped",
            "visible_object": "vehicle-between-lanes dashboard button",
            "interpretation": "supports",
            "expected_state": "visually_supported",
            "verification_method": "original window sticker on file",
            "ambiguity": "night photo; button backlight glare present",
        },
    }
    case.update(overrides)
    return case


def _valid_manifest(tmp_path):
    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    blob = b"fake-image-bytes"
    photo = photos_dir / "case-001.jpg"
    photo.write_bytes(blob)
    import hashlib

    digest = hashlib.sha256(blob).hexdigest()
    return {
        "manifest_version": MANIFEST_VERSION,
        "dataset": "local-pilot",
        "split": "dev",
        "cases": [
            _case(
                id="case-001",
                photo={
                    "path": "photos/case-001.jpg",
                    "sha256": digest,
                    "provenance": "evaluator photo",
                },
            )
        ],
    }


def test_valid_manifest_passes(tmp_path):
    manifest = _valid_manifest(tmp_path)
    checked = validate_manifest(manifest, photos_root=tmp_path)
    assert checked[0]["id"] == "case-001"


def test_manifest_version_rejected(tmp_path):
    manifest = _valid_manifest(tmp_path)
    manifest["manifest_version"] = "v0"
    with pytest.raises(ValueError, match="manifest_version"):
        validate_manifest(manifest, photos_root=tmp_path)


def test_sha256_mismatch_rejected(tmp_path):
    manifest = _valid_manifest(tmp_path)
    manifest["cases"][0]["photo"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="sha256 mismatch"):
        validate_manifest(manifest, photos_root=tmp_path)


def test_missing_photo_rejected(tmp_path):
    manifest = _valid_manifest(tmp_path)
    manifest["cases"][0]["photo"]["path"] = "photos/does-not-exist.jpg"
    with pytest.raises(ValueError, match="missing photo"):
        validate_manifest(manifest, photos_root=tmp_path)


def test_duplicate_case_ids_rejected(tmp_path):
    manifest = _valid_manifest(tmp_path)
    manifest["cases"].append(dict(manifest["cases"][0]))
    with pytest.raises(ValueError, match="duplicate case id"):
        validate_manifest(manifest, photos_root=tmp_path)


def test_unknown_expected_state_rejected():
    case = _case()
    case["ground_truth"]["expected_state"] = "probably-present"
    with pytest.raises(ValueError, match="expected_state"):
        validate_case(case)


def test_bad_photo_path_rejected():
    case = _case()
    case["photo"]["path"] = "../escape.jpg"
    with pytest.raises(ValueError, match="photo.path"):
        validate_case(case)


def test_neither_split_value_rejected(tmp_path):
    manifest = _valid_manifest(tmp_path)
    manifest["split"] = "dev-only"
    with pytest.raises(ValueError, match="split"):
        validate_manifest(manifest, photos_root=tmp_path)


def test_scoring_hallucinated_positive_detection():
    case = _case(
        ground_truth={
            "equipment": "not-equipped",
            "visible_object": "generic cruise on/off button",
            "interpretation": "generic",
            "expected_state": "insufficient_evidence",
            "verification_method": "base-trim spec sheet",
            "ambiguity": "",
        }
    )
    recorded = {
        "case_id": "test-001",
        "mode": "baseline",
        "state": "visually_supported",
        "prompt_version": "m1",
        "control": "following-distance button",
        "interpretation": "supports",
        "sam_target": "Following-distance adjustment button",
    }
    row = score_case(case, recorded)
    assert row["hallucinated_positive"] is True
    assert row["state_correct"] is False


def test_scoring_unsupported_equipped_flag():
    """An equipped vehicle with an unreadable photo: claiming visual support
    over-claims beyond the visible evidence (unsupported), but it is not a
    hallucination of absent equipment."""
    case = _case(
        ground_truth={
            "equipment": "equipped",
            "visible_object": "blurred dashboard area, control unreadable",
            "interpretation": "insufficient",
            "expected_state": "insufficient_evidence",
            "verification_method": "original window sticker on file",
            "ambiguity": "heavy motion blur",
        }
    )
    recorded = {
        "case_id": "test-001",
        "mode": "reference",
        "state": "visually_supported",
        "prompt_version": "m2",
        "control": "lane assistance button",
        "interpretation": "supports",
        "sam_target": "Lane assistance button",
    }
    row = score_case(case, recorded)
    assert row["unsupported_equipped"] is True
    assert row["hallucinated_positive"] is False
    assert row["state_correct"] is False


def test_scoring_missing_and_unreadable_statuses():
    case = _case()
    assert score_case(case, {"status": "missing-photo"})["status"] == "missing-photo"
    assert score_case(case, {"status": "unreadable"})["status"] == "unreadable"


def test_scoring_generic_control_no_positive():
    case = _case()
    recorded = {
        "case_id": "test-001",
        "mode": "baseline",
        "state": "insufficient_evidence",
        "prompt_version": "m1",
        "control": "generic cruise buttons",
        "interpretation": "insufficient",
        "sam_target": "Cruise control buttons",
    }
    row = score_case(case, recorded)
    assert row["predicted_positive"] is False
    assert row["state_correct"] is False


def test_summarize_paired_modes_present():
    base_rows = [
        {
            "case_id": "a",
            "mode": "baseline",
            "status": "scored",
            "state": "insufficient_evidence",
            "control_correct": True,
            "predicted_positive": True,
            "truth_positive": True,
            "support_correct": True,
            "state_correct": True,
        },
        {
            "case_id": "a",
            "mode": "reference",
            "status": "scored",
            "state": "visually_supported",
            "control_correct": True,
            "predicted_positive": True,
            "truth_positive": True,
            "support_correct": True,
            "state_correct": True,
        },
    ]
    summary = summarize("m", base_rows)
    assert summary["paired_cases"] == 1
    assert summary["modes"]["baseline"]["n"] == 1
    assert summary["modes"]["baseline"]["insufficient_unknown_rate"] == 1.0
    assert summary["modes"]["reference"]["support_correct"] == 1
    assert summary["modes"]["reference"]["insufficient_unknown_rate"] == 0.0

    flagged = dict(base_rows[0])
    flagged["hallucinated_positive"] = True
    summary = summarize("m", [flagged, base_rows[1]])
    assert summary["modes"]["baseline"]["hallucinated_positive"] == 1
    assert summary["modes"]["baseline"]["insufficient_unknown_rate"] == 1.0


def test_estimate_spend_shape():
    assert estimate_spend(10, muse_calls_per_case=2, sam_calls_per_case=2) == {
        "cases": 10,
        "modes": 2,
        "jobs": 20,
        "muse_calls_planned": 40,
        "sam_calls_planned": 40,
        "worst_case_usd": "40.00",
        "spend_cap_usd_per_job": "2.00",
    }


def test_fixture_manifest_validates():
    root = Path(__file__).resolve().parents[2]
    manifest_path = root / "fixtures" / "realworld-sample" / "manifest.json"
    cases = validate_manifest(
        manifest_path, photos_root=root / "fixtures" / "realworld-sample"
    )
    assert {c["id"] for c in cases} == {"synth-acc-001", "synth-cruise-001"}


def test_render_markdown_has_denominators():
    case = _case()
    rows = [
        score_case(
            case,
            {
                "case_id": "test-001",
                "mode": mode,
                "state": "visually_supported",
                "prompt_version": "m1",
                "control": "vehicle-between-lanes dashboard button",
                "interpretation": "supports",
                "sam_target": "Lane assistance button",
            },
        )
        for mode in ("baseline", "reference")
    ]
    summary = summarize("m", rows)
    md = render_markdown("m", summary, rows)
    assert "0/1" in md  # zero false-support count with denominator
    assert "1.00 (1)" in md  # perfect control accuracy with denominator
    assert "paired_cases=1" in md
