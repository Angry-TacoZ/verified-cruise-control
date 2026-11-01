"""Unit tests for the M3/M4 real-world evaluation scaffold.

Covers manifest validation, deterministic scoring, and spend estimation.
All tests are offline; no provider calls are made.
"""

from pathlib import Path

import pytest

from audits.realworld import (
    MANIFEST_VERSION,
    BudgetRefused,
    EvalBudget,
    build_eval_observation,
    estimate_spend,
    muse_verdict,
    render_markdown,
    score_case,
    summarize,
    validate_case,
    validate_manifest,
    write_ledger,
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


def _case_with_localization(outcome):
    truth = dict(
        _case()["ground_truth"],
        localization={
            "verified_outcome": outcome,
            "verified_by": "human review of SAM masks",
        },
    )
    return _case(ground_truth=truth)


def test_localization_block_validates():
    case = _case_with_localization("localized")
    assert validate_case(case)["id"] == "test-001"


def test_localization_block_bad_outcome_rejected():
    case = _case_with_localization("maybe")
    try:
        validate_case(case)
    except ValueError:
        pass
    else:
        raise AssertionError("bad verified_outcome should be rejected")


def test_localization_block_missing_method_rejected():
    case = _case_with_localization("localized")
    del case["ground_truth"]["localization"]["verified_by"]
    try:
        validate_case(case)
    except ValueError:
        pass
    else:
        raise AssertionError("localization without verified_by should be rejected")


def _recorded_with_sam(masks, target="vehicle-between-lanes dashboard button"):
    return {
        "case_id": "test-001",
        "mode": "baseline",
        "status": "scored",
        "state": "visually_supported",
        "prompt_version": "m2",
        "control": target,
        "interpretation": "supports",
        "sam_target": target,
        "sam_masks": masks,
    }


def test_sam_localization_scored_only_with_ground_truth():
    # F3: masks>0 with a verified "localized" label scores True.
    row = score_case(_case_with_localization("localized"), _recorded_with_sam(2))
    assert row["sam_localization_correct"] is True
    # Masks>0 against a verified "no-mask" label scores False.
    row = score_case(_case_with_localization("no-mask"), _recorded_with_sam(1))
    assert row["sam_localization_correct"] is False
    # No masks against a verified "localized" label scores False.
    row = score_case(_case_with_localization("localized"), _recorded_with_sam(0))
    assert row["sam_localization_correct"] is False
    # Without an independent label, localization is not measured — never zero.
    row = score_case(_case(), _recorded_with_sam(3))
    assert row["sam_localization_correct"] is None


def test_sam_target_selection_is_not_localization():
    # F3: naming the GT object is selection correctness (text agreement),
    # not proof the region was localized.
    row = score_case(_case(), _recorded_with_sam(0))
    assert row["sam_target_selected"] is True
    assert row["sam_localization_correct"] is None
    row = score_case(_case(), _recorded_with_sam(0, target="unrelated volume knob"))
    assert row["sam_target_selected"] is False


def test_sam_localization_unmeasured_without_labels():
    rows = [score_case(_case(), _recorded_with_sam(2))]
    summary = summarize("m", rows)
    assert summary["modes"]["baseline"]["sam_localization_accuracy"] is None
    md = render_markdown("m", summary, rows)
    assert "not measured" in md


class _FakeAdapter:
    """Deterministic stand-in for a provider adapter (no network, no keys)."""

    provider_name = "fake"

    def __init__(self, cost="0.01", retries=0, fail=False):
        from types import SimpleNamespace

        self.calls = []
        self.usage = SimpleNamespace(
            provider="fake",
            estimated_cost_usd=cost,
            retries=retries,
            status="ok",
        )
        self.fail = fail
        self.last_attempts = 1 + retries

    def analyze(self, request):
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("provider blew up")
        from types import SimpleNamespace

        return SimpleNamespace(observations=[], usage=self.usage)


def _budget(**overrides):
    kwargs = {
        "authorized": True,
        "max_spend_usd": "5.00",
        "max_attempts_total": 12,
        "reserve_usd_per_call": "2.00",
    }
    kwargs.update(overrides)
    return EvalBudget(**kwargs)


def test_budget_refuses_without_authorization():
    # F1: no authorization flag, no call — the adapter must never fire.
    adapter = _FakeAdapter()
    budget = _budget(authorized=False)
    try:
        budget.guarded("Muse case mode", adapter, object(), max_attempts=3)
    except BudgetRefused:
        pass
    else:
        raise AssertionError("unauthorized call should be refused")
    assert adapter.calls == []


def test_budget_refuses_over_spend_headroom():
    # F1: reservation exceeding remaining spend is refused before sending.
    adapter = _FakeAdapter()
    budget = _budget(max_spend_usd="1.00", reserve_usd_per_call="2.00")
    try:
        budget.guarded("Muse case mode", adapter, object(), max_attempts=3)
    except BudgetRefused:
        pass
    else:
        raise AssertionError("over-budget call should be refused")
    assert adapter.calls == []


def test_budget_counts_retry_headroom_against_call_cap():
    # F1: max_attempts (first try + retries) comes out of the call allowance.
    budget = _budget(max_attempts_total=3)
    token = budget.reserve("first", max_attempts=3)
    assert token is not None
    try:
        budget.reserve("second", max_attempts=1)
    except BudgetRefused:
        pass
    else:
        raise AssertionError("call beyond the attempt allowance should be refused")
    budget.settle(token, attempts=1, cost_usd="0.01", provider="fake")
    # Released headroom allows exactly one more single attempt.
    budget.reserve("second", max_attempts=1)


def test_budget_unknown_cost_freezes_further_spending():
    # F1: unknown billing is never zero — it freezes the ledger.
    budget = _budget()
    token = budget.reserve("flaky", max_attempts=1)
    budget.settle(token, attempts=1, cost_usd=None, provider="fake")
    try:
        budget.reserve("next", max_attempts=1)
    except BudgetRefused:
        pass
    else:
        raise AssertionError("spend after unknown cost should be refused")


def test_budget_ledger_survives_interruption(tmp_path):
    # F1: a reservation written before a crash stays visible afterwards.
    import json

    budget = _budget()
    budget.reserve("interrupted-call", max_attempts=2)
    path = tmp_path / "ledger-probe.jsonl"
    write_ledger(path, budget)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "reserved"
    assert entry["outstanding"] is True


def test_guarded_settles_actuals_and_releases_headroom():
    adapter = _FakeAdapter(cost="0.01", retries=1)
    budget = _budget(max_spend_usd="5.00", reserve_usd_per_call="2.00")
    # Reserve-then-settle across two guarded calls stays within budget.
    budget.guarded("one", adapter, object(), max_attempts=3)
    budget.guarded("two", adapter, object(), max_attempts=3)
    assert adapter.calls != []
    totals = budget.totals()
    assert totals["spent_usd"] == "0.02"
    assert totals["attempts_used"] == 4


FORTE_2021 = {
    "year": 2021,
    "make": "Kia",
    "model": "Forte",
    "trim": "GT-Line",
    "market": "US",
}


def _acc_obs(nested=True):
    obs = {
        "photo_id": "test-001",
        "claim_concept": "acc",
        "visible_object": "cruise control buttons",
        "description": "steering-wheel cruise cluster, no distance markings visible",
        "control_symbol": "",
        "control_type": "button",
        "location": "steering_wheel",
        "dedicated": False,
        "manufacturer_context": "",
        "supports_localization": True,
        "ambiguous": False,
        "needs_crop": False,
        "needs_other_view": False,
    }
    if nested:
        obs["controls"] = [
            {
                "visible_object": "following-distance adjustment button",
                "control_symbol": "vehicle with distance bars",
                "control_type": "button",
                "location": "steering_wheel",
                "dedicated": True,
            }
        ]
    return obs


def test_muse_verdict_mapping():
    # Production-identical mapping: ambiguity is unclear, literal "none"
    # is absent, anything else is present.
    assert muse_verdict({"ambiguous": True, "visible_object": "button"}) == "unclear"
    assert muse_verdict({"visible_object": "none"}) == "absent"
    assert muse_verdict({"visible_object": "None"}) == "absent"
    assert muse_verdict({"visible_object": "cruise buttons"}) == "present"
    assert muse_verdict({}) == "present"


def test_eval_observation_carries_full_control_identification():
    # F2: the eval observation must preserve symbols, locations,
    # dedicated flags, and nested controls — not just visible_object.
    from audits.pipeline import reconcile_claim

    assert reconcile_claim is not None
    row = build_eval_observation(_acc_obs(), claim_id="acc", photo_id="test-001")
    assert row.extra["control_symbol"] == ""
    assert row.extra["location"] == "steering_wheel"
    assert row.extra["dedicated"] is False
    assert row.extra["controls"][0]["visible_object"] == (
        "following-distance adjustment button"
    )
    assert row.extra["controls"][0]["dedicated"] is True
    assert row.verdict == "present"
    assert row.rationale.startswith("steering-wheel cruise cluster")


def test_eval_nested_acc_control_changes_expected_verdict():
    # F2: an ACC-relevant nested control must change the reconciled verdict.
    from audits.pipeline import reconcile_claim

    claim = "Adaptive Cruise Control"
    with_nested = reconcile_claim(
        [build_eval_observation(_acc_obs(nested=True), claim_id="acc", photo_id="p")],
        claim,
        FORTE_2021,
    )[0]
    without_nested = reconcile_claim(
        [build_eval_observation(_acc_obs(nested=False), claim_id="acc", photo_id="p")],
        claim,
        FORTE_2021,
    )[0]
    assert with_nested == "visually_supported"
    assert without_nested == "insufficient_evidence"


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


def _scored_with(control, visible):
    case = _case(
        ground_truth={
            "equipment": "equipped",
            "visible_object": visible,
            "interpretation": "supports",
            "expected_state": "visually_supported",
            "verification_method": "method",
            "ambiguity": "",
        }
    )
    return score_case(
        case,
        {
            "case_id": "test-001",
            "mode": "baseline",
            "status": "scored",
            "state": "visually_supported",
            "prompt_version": "m1",
            "control": control,
            "interpretation": "supports",
        },
    )


def test_control_match_empty_ground_truth_never_matches():
    # F4: an empty independently established object must not match any
    # nonempty prediction (old substring logic returned True via `in`).
    assert _scored_with("following-distance button", "")["control_correct"] is False
    assert _scored_with("", "following-distance button")["control_correct"] is False
    assert _scored_with("", "")["control_correct"] is False


def test_control_match_single_generic_token_no_match():
    # F4: one shared generic token ("button") must not equate unrelated
    # controls (old logic: "button" in "following-distance button").
    row = _scored_with("button", "following-distance button")
    assert row["control_correct"] is False
    row = _scored_with("volume button", "following-distance adjustment button")
    assert row["control_correct"] is False


def test_control_match_case_whitespace_and_subset():
    assert (
        _scored_with("  Lane Assistance Button ", "lane assistance button")[
            "control_correct"
        ]
        is True
    )
    # Plural-insensitive subset with two shared substantive tokens.
    assert (
        _scored_with("cruise buttons", "cruise control buttons")["control_correct"]
        is True
    )
    # Hyphenation differences do not matter when two substantive tokens match.
    assert (
        _scored_with("following distance button", "following-distance button")[
            "control_correct"
        ]
        is True
    )
    # Near-miss wording with a single shared substantive token is not credit.
    assert (
        _scored_with("distance button", "following-distance button")["control_correct"]
        is False
    )
