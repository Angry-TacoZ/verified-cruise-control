"""Unit tests for the M3/M4 real-world evaluation scaffold.

Covers manifest validation, deterministic scoring, and spend estimation.
All tests are offline; no provider calls are made.
"""

import json
from pathlib import Path

import pytest

from audits.realworld import (
    MANIFEST_VERSION,
    BudgetRefused,
    EvalBudget,
    LedgerFile,
    acquire_ledger_lock,
    assert_ledger_clear,
    build_eval_observation,
    build_sam_eval_observation,
    dataset_fingerprint,
    estimate_spend,
    find_outstanding,
    fresh_ledger_path,
    ledger_lock_path,
    muse_verdict,
    read_ledger,
    release_ledger_lock,
    render_markdown,
    scan_ledger_dir,
    score_case,
    select_sam_target,
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


def test_reservation_carries_call_identity(tmp_path):
    # F1: ledger records carry run/call identity, provider, model, and the
    # authorized attempt allowance — never keys, bytes, or personal data.
    import json

    ledger_path = tmp_path / "identity.jsonl"
    handle = LedgerFile(ledger_path)
    budget = _budget_with_file(handle)
    budget.guarded("Muse case mode", _FakeAdapter(), object(), max_attempts=3)
    handle.close()
    records = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
    ]
    reserved = records[0]
    assert reserved["event"] == "reserved"
    assert reserved["run_id"] == budget.run_id
    assert reserved["label"] == "Muse case mode"
    assert reserved["max_attempts"] == 3
    assert reserved["reserve_usd"] == "2.00"
    assert reserved["provider"] == "fake"
    # No keys, bytes, images, or personal data may enter the ledger.
    blob = json.dumps(records).lower()
    for forbidden in ("api_key", "base64", "data:image", "vin", "plate"):
        assert forbidden not in blob


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


def _budget_with_file(ledger_file, **overrides):
    kwargs = {
        "authorized": True,
        "max_spend_usd": "5.00",
        "max_attempts_total": 12,
        "reserve_usd_per_call": "2.00",
        "ledger_file": ledger_file,
    }
    kwargs.update(overrides)
    return EvalBudget(**kwargs)


def test_reservation_on_disk_before_analyze(tmp_path):
    # F1: the reservation must be fsync'd to disk before the adapter fires.
    import json

    ledger_path = tmp_path / "run.jsonl"
    seen = {}

    class CheckingAdapter(_FakeAdapter):
        def analyze(self, request):
            lines = ledger_path.read_text(encoding="utf-8").splitlines()
            seen["reserved"] = [
                json.loads(line)
                for line in lines
                if json.loads(line)["event"] == "reserved"
            ]
            return super().analyze(request)

    adapter = CheckingAdapter()
    handle = LedgerFile(ledger_path)
    try:
        budget = _budget_with_file(handle)
        budget.guarded("Muse case mode", adapter, object(), max_attempts=3)
    finally:
        handle.close()
    assert adapter.calls != []
    assert len(seen["reserved"]) == 1
    assert seen["reserved"][0]["label"] == "Muse case mode"
    assert seen["reserved"][0]["max_attempts"] == 3


def test_reservation_persistence_failure_prevents_call():
    # F1: if the reservation cannot be persisted, no provider call happens.
    class BrokenFile:
        def append(self, record):
            raise OSError("disk gone")

    adapter = _FakeAdapter()
    budget = _budget_with_file(BrokenFile())
    with pytest.raises(OSError):
        budget.guarded("Muse case mode", adapter, object(), max_attempts=3)
    assert adapter.calls == []


def test_crash_leaves_outstanding_and_restart_refuses(tmp_path):
    # F1: simulate hard termination between reservation and settlement,
    # then prove a restart detects it and refuses to repeat the call.
    ledger_path = tmp_path / "crashed.jsonl"
    handle = LedgerFile(ledger_path)
    budget = _budget_with_file(handle)
    budget.reserve("Muse doomed mode", max_attempts=3)
    handle.close()  # crash: settlement is never written
    records = read_ledger(ledger_path)
    assert find_outstanding(records) != []
    with pytest.raises(BudgetRefused, match="doomed"):
        assert_ledger_clear(ledger_path)
    # A fully settled ledger restarts cleanly.
    clean_path = tmp_path / "clean.jsonl"
    clean_handle = LedgerFile(clean_path)
    clean_budget = _budget_with_file(clean_handle)
    token = clean_budget.reserve("Muse ok mode", max_attempts=1)
    clean_budget.settle(token, attempts=1, cost_usd="0.01", provider="fake")
    clean_handle.close()
    assert_ledger_clear(clean_path)


def test_known_cost_settlement_persisted(tmp_path):
    # F1: settlements land on disk with actual cost, attempts, provider.
    import json

    ledger_path = tmp_path / "settled.jsonl"
    handle = LedgerFile(ledger_path)
    budget = _budget_with_file(handle)
    token = budget.reserve("Muse priced mode", max_attempts=2)
    budget.settle(token, attempts=2, cost_usd="0.03", provider="fake")
    handle.close()
    events = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["event"] for entry in events] == ["reserved", "settled"]
    assert events[1]["cost_usd"] == "0.03"
    assert events[1]["attempts"] == 2
    assert events[1]["provider"] == "fake"


def test_ledger_runs_do_not_overwrite(tmp_path):
    # F1: unique run ledger files never overwrite one another.
    first = fresh_ledger_path(tmp_path, "realworld-live", "runAAA")
    second = fresh_ledger_path(tmp_path, "realworld-live", "runBBB")
    assert first != second
    LedgerFile(first).append({"event": "reserved", "label": "first"})
    second_handle = LedgerFile(second)
    second_handle.append({"event": "reserved", "label": "second"})
    second_handle.close()
    import json

    assert json.loads(first.read_text(encoding="utf-8"))["label"] == "first"
    # Same run id + stem resolves to the same path (explicit resume target).
    assert fresh_ledger_path(tmp_path, "realworld-live", "runAAA") == first


def _sam_scenario_obs(top_visible, nested, supports_localization=True):
    obs = {
        "photo_id": "photo-1",
        "claim_concept": "c1",
        "visible_object": top_visible,
        "description": "eval scenario",
        "control_symbol": "",
        "control_type": "",
        "location": "unknown",
        "dedicated": None,
        "manufacturer_context": "",
        "supports_localization": supports_localization,
        "ambiguous": False,
        "needs_crop": False,
        "needs_other_view": False,
    }
    if nested is not None:
        obs["controls"] = [nested]
    return obs


def _acc_nested_item():
    return {
        "visible_object": "following-distance button",
        "control_symbol": "vehicle with distance bars",
        "control_type": "button",
        "location": "steering_wheel",
        "dedicated": True,
    }


def _lka_nested_item():
    return {
        "visible_object": "lane keeping assist button",
        "control_symbol": "vehicle between lane lines",
        "control_type": "button",
        "location": "left_dashboard",
        "dedicated": True,
    }


def test_sam_selects_nested_acc_control():
    # F2 scenario A: the nested dedicated following-distance control wins
    # over generic top-level cruise buttons, with its control index.
    obs = build_eval_observation(
        _sam_scenario_obs("cruise control buttons", _acc_nested_item()),
        claim_id="acc",
        photo_id="photo-1",
    )
    selected = select_sam_target(obs, "Adaptive Cruise Control", FORTE_2021)
    assert selected == ("following-distance button", "observed_object", 1)


def test_sam_selects_nested_control_over_cupholder():
    # F2 scenario B: an unrelated top-level object never becomes the target
    # when a claim-relevant nested control exists.
    obs = build_eval_observation(
        _sam_scenario_obs("cup holder", _lka_nested_item()),
        claim_id="lka",
        photo_id="photo-1",
    )
    selected = select_sam_target(obs, "Lane Keeping Assist", FORTE_2021)
    assert selected == ("lane keeping assist button", "observed_object", 1)


def test_sam_no_target_without_identifiable_control():
    # F2 scenario C: no identifiable control means no SAM target.
    obs = build_eval_observation(
        _sam_scenario_obs("none", None, supports_localization=False),
        claim_id="acc",
        photo_id="photo-1",
    )
    assert select_sam_target(obs, "Adaptive Cruise Control", FORTE_2021) is None


def _load_eval_runner():
    import importlib.util

    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("eval_run", repo / "evals" / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _harness_manifest(tmp_path):
    import hashlib

    photos = tmp_path / "photos"
    photos.mkdir()
    src = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "realworld-sample"
        / "photos"
        / "synth-acc-001.png"
    )
    data = src.read_bytes()
    (photos / "case-a.png").write_bytes(data)
    (photos / "case-b.png").write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()

    def case(cid):
        return {
            "id": cid,
            "vehicle": {
                "year": 2021,
                "make": "Kia",
                "model": "Forte",
                "trim": "GT",
                "market": "US",
            },
            "claim": {
                "advertised": "Adaptive Cruise Control",
                "normalized_feature": "adaptive_cruise_control",
                "claim_id": "acc",
            },
            "photo": {
                "path": "photos/placeholder.png",
                "sha256": sha,
                "provenance": "synthetic",
            },
            "ground_truth": {
                "equipment": "unknown",
                "visible_object": "synthetic control",
                "interpretation": "insufficient",
                "expected_state": "insufficient_evidence",
                "verification_method": "fixture",
                "ambiguity": "",
            },
        }

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "dataset": "harness-probe",
        "split": "dev",
        "cases": [case("case-a"), case("case-b")],
    }
    manifest["cases"][0]["photo"]["path"] = "photos/case-a.png"
    manifest["cases"][1]["photo"]["path"] = "photos/case-b.png"
    return manifest


class _HarnessMuseAdapter:
    provider_name = "fake-muse"
    model_id = "fake-muse-1"

    def __init__(self):
        from types import SimpleNamespace

        self.calls = []
        self.usage = SimpleNamespace(
            provider="fake-muse", estimated_cost_usd="0.01", retries=0, status="ok"
        )
        self.last_attempts = 1

    def require_configured(self):
        pass

    def analyze(self, request):
        from types import SimpleNamespace

        self.calls.append(request)
        photo_id = request.photo_ids[0]
        if photo_id == "case-a":
            obs = {
                "visible_object": "none",
                "supports_localization": False,
                "ambiguous": False,
            }
        else:
            obs = {
                "visible_object": "cruise control buttons",
                "supports_localization": False,
                "ambiguous": False,
            }
        return SimpleNamespace(observations=[obs], usage=self.usage)


class _HarnessSamAdapter:
    provider_name = "fake-sam"
    model_id = "fake-sam-1"

    def __init__(self):
        self.calls = []

    def require_configured(self):
        pass

    def analyze(self, request):
        raise AssertionError("SAM must not be called without a valid target")


def test_harness_makes_no_sam_call_without_target(tmp_path, monkeypatch):
    # F2 scenario C end to end: no identifiable control and no localization
    # support means zero SAM requests, explicitly recorded — with no paid
    # inference anywhere in the run.
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    manifest = _harness_manifest(tmp_path)
    report = runner.run_realworld_live(
        manifest,
        tmp_path,
        10.0,
        True,
        muse_adapter=fake_muse,
        sam_adapter=fake_sam,
        results_dir=tmp_path / "results",
        ledger_path=tmp_path / "ledgers" / "probe.jsonl",
    )
    assert fake_muse.calls != []
    assert fake_sam.calls == []
    assert {row["sam_status"] for row in report["raw"]} == {"unavailable-target"}
    assert {row["status"] for row in report["rows"]} == {"scored"}
    assert report["spend_ledger"]["attempts_used"] == 4
    assert report["spend_ledger"]["unknown_cost"] is False
    assert report["spend_ledger"]["outstanding"] == []


def test_sam_wrong_photo_never_corroborates():
    # F2 scenario D: a SAM localization from a different photo/control must
    # not corroborate; provenance stays correctly associated.
    from audits.pipeline import reconcile_claim

    claim = "Adaptive Cruise Control"
    muse_obs = build_eval_observation(
        _sam_scenario_obs("cruise control buttons", _acc_nested_item()),
        claim_id="acc",
        photo_id="photo-1",
    )
    wrong_sam = build_sam_eval_observation(
        muse_obs,
        concept="following-distance button",
        target_source="observed_object",
        control_index=0,  # wrong item: the mapping stands on item 1
        masks=2,
    )
    wrong_sam.photo_id = __import__("uuid").uuid4()  # wrong photo
    state, summary = reconcile_claim([muse_obs, wrong_sam], claim, FORTE_2021)
    assert state == "visually_supported"
    assert "different photo or control" in summary
    right_sam = build_sam_eval_observation(
        muse_obs,
        concept="following-distance button",
        target_source="observed_object",
        control_index=1,
        masks=2,
    )
    state, summary = reconcile_claim([muse_obs, right_sam], claim, FORTE_2021)
    assert state == "visually_supported"
    assert "confirmed by SAM localization" in summary


def _harness_run(tmp_path, manifest, monkeypatch):
    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    report = runner.run_realworld_live(
        manifest,
        tmp_path,
        10.0,
        True,
        muse_adapter=fake_muse,
        sam_adapter=fake_sam,
        results_dir=tmp_path / "results",
    )
    return report, fake_muse, fake_sam


def _crashed_ledger_for(manifest, directory, run_id="runDEAD"):
    """Simulate a hard crash: reservation persisted, never settled."""
    fingerprint = dataset_fingerprint(manifest)
    path = directory / f"2026-01-01-realworld-live-{run_id}.jsonl"
    handle = LedgerFile(path)
    budget = EvalBudget(
        authorized=True,
        max_spend_usd="5.00",
        max_attempts_total=12,
        reserve_usd_per_call="2.00",
        ledger_file=handle,
        dataset_id=fingerprint,
    )
    budget.reserve(
        "Muse case-a baseline",
        max_attempts=3,
        provider="fake-muse",
        case_id="case-a",
        mode="baseline",
    )
    handle.close()
    return path, fingerprint


def test_default_restart_refuses_unresolved(tmp_path, monkeypatch):
    # Test A: restarting the same evaluation with the default command
    # (no --ledger) must refuse before invoking any provider.
    manifest = _harness_manifest(tmp_path)
    ledgers = tmp_path / "results"
    ledgers.mkdir()
    _crashed_ledger_for(manifest, ledgers)
    assert len(scan_ledger_dir(ledgers)) == 1
    import pytest as _pytest

    with _pytest.raises(SystemExit, match="outstanding"):
        _harness_run(tmp_path, manifest, monkeypatch)


def test_default_restart_makes_no_provider_call(tmp_path, monkeypatch):
    # Test A (provider silence): the refused restart never fires adapters.
    manifest = _harness_manifest(tmp_path)
    ledgers = tmp_path / "results"
    ledgers.mkdir()
    _crashed_ledger_for(manifest, ledgers)
    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    import pytest as _pytest

    with _pytest.raises(SystemExit):
        runner.run_realworld_live(
            manifest,
            tmp_path,
            10.0,
            True,
            muse_adapter=fake_muse,
            sam_adapter=fake_sam,
            results_dir=tmp_path / "results",
        )
    assert fake_muse.calls == []
    assert fake_sam.calls == []


def test_clean_prior_run_does_not_block(tmp_path, monkeypatch):
    # Test B: a fully settled previous run must not block the next one.
    manifest = _harness_manifest(tmp_path)
    ledgers = tmp_path / "results"
    ledgers.mkdir()
    fingerprint = dataset_fingerprint(manifest)
    path = ledgers / "2026-01-01-realworld-live-runOK.jsonl"
    handle = LedgerFile(path)
    budget = EvalBudget(
        authorized=True,
        max_spend_usd="5.00",
        max_attempts_total=12,
        reserve_usd_per_call="2.00",
        ledger_file=handle,
        dataset_id=fingerprint,
    )
    token = budget.reserve("Muse case-a baseline", max_attempts=1)
    budget.settle(token, attempts=1, cost_usd="0.01", provider="fake-muse")
    handle.close()
    report, fake_muse, _unused = _harness_run(tmp_path, manifest, monkeypatch)
    assert fake_muse.calls != []
    assert report["spend_ledger"]["unknown_cost"] is False


def test_explicit_ledger_still_detects_outstanding(tmp_path, monkeypatch):
    # Test C: --ledger behavior is preserved for explicitly supplied files.
    manifest = _harness_manifest(tmp_path)
    explicit = tmp_path / "explicit.jsonl"
    _crashed_ledger_for(manifest, tmp_path, run_id="runEXPL")
    (tmp_path / "2026-01-01-realworld-live-runEXPL.jsonl").rename(explicit)
    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    import pytest as _pytest

    with _pytest.raises(SystemExit, match="outstanding"):
        runner.run_realworld_live(
            manifest,
            tmp_path,
            10.0,
            True,
            muse_adapter=fake_muse,
            sam_adapter=fake_sam,
            results_dir=tmp_path / "results",
            ledger_path=explicit,
        )
    assert fake_muse.calls == []


def _different_manifest(manifest_a):
    import copy

    manifest = copy.deepcopy(manifest_a)
    manifest["dataset"] = "other-probe"
    manifest["cases"][0]["id"] = "case-z"
    return manifest


def test_different_dataset_outstanding_blocks(tmp_path, monkeypatch):
    # Test D (updated for the directory-wide guard): a crashed run blocks
    # a new evaluation even for a demonstrably different dataset, because
    # the unresolved reservation already left the provider account.
    manifest_a = _harness_manifest(tmp_path)
    ledgers = tmp_path / "results"
    ledgers.mkdir()
    _crashed_ledger_for(manifest_a, ledgers, run_id="runAAAA")
    manifest_b = _different_manifest(manifest_a)
    assert dataset_fingerprint(manifest_a) != dataset_fingerprint(manifest_b)
    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    import pytest as _pytest

    with _pytest.raises(SystemExit, match="outstanding"):
        runner.run_realworld_live(
            manifest_b,
            tmp_path,
            10.0,
            True,
            muse_adapter=fake_muse,
            sam_adapter=fake_sam,
            results_dir=tmp_path / "results",
        )
    assert fake_muse.calls == []
    assert fake_sam.calls == []


def test_malformed_ledger_fails_closed(tmp_path, monkeypatch):
    # Test E: corrupted or ambiguous relevant records refuse inference.
    manifest = _harness_manifest(tmp_path)
    ledgers = tmp_path / "results"
    ledgers.mkdir()
    (ledgers / "2026-01-01-realworld-live-runBAD.jsonl").write_text(
        '{"event": "reserved", "label": "Muse case-a baseline"\n', encoding="utf-8"
    )
    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    import pytest as _pytest

    with _pytest.raises(SystemExit, match="unreadable|malformed|outstanding"):
        runner.run_realworld_live(
            manifest,
            tmp_path,
            10.0,
            True,
            muse_adapter=fake_muse,
            sam_adapter=fake_sam,
            results_dir=tmp_path / "results",
        )
    assert fake_muse.calls == []
    # A reserved record without a token can never match a settlement.
    assert find_outstanding([{"event": "reserved", "label": "x"}]) != []


def test_concurrent_start_single_winner(tmp_path):
    # Test F: two simultaneous attempts sharing a results directory cannot
    # both pass the preflight coordination gate. The lock is an OS-held
    # file descriptor with no TTL, so there is nothing to expire.
    lock = ledger_lock_path(tmp_path)
    held = acquire_ledger_lock(lock, "runA")
    import pytest as _pytest

    with _pytest.raises(BudgetRefused):
        acquire_ledger_lock(lock, "runB")
    # A foreign release request never releases someone else's lock.
    release_ledger_lock(lock, "not-runA")
    with _pytest.raises(BudgetRefused):
        acquire_ledger_lock(lock, "runC")
    held.release()
    # After a clean release the next run proceeds. The lock file itself
    # is intentionally left on disk (diagnostics only, never unlinked).
    follow = acquire_ledger_lock(lock, "runD")
    follow.release()
    assert lock.exists()


def test_concurrent_run_refuses_while_locked(tmp_path, monkeypatch):
    # Test F (integration): a run started while another holds the
    # directory lock refuses before any provider request.
    manifest = _harness_manifest(tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    other = acquire_ledger_lock(ledger_lock_path(results), "runOTHER")
    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    import pytest as _pytest

    try:
        with _pytest.raises(
            SystemExit, match="another evaluation|lock|outstanding|holds"
        ):
            runner.run_realworld_live(
                manifest,
                tmp_path,
                10.0,
                True,
                muse_adapter=fake_muse,
                sam_adapter=fake_sam,
                results_dir=results,
            )
    finally:
        other.release()
    assert fake_muse.calls == []
    assert fake_sam.calls == []


def test_stale_lock_content_never_blocks(tmp_path):
    # F1: lock-file content (timestamps, run ids) is diagnostics only.
    # Stale, ancient, or garbage content with no live holder never
    # blocks the next run; ownership is proved by the OS lock alone.
    import json as _json
    import time as _time

    lock = ledger_lock_path(tmp_path)
    lock.write_text(
        _json.dumps({"run_id": "runCRASH", "pid": 1, "created": _time.time() - 7200}),
        encoding="utf-8",
    )
    first = acquire_ledger_lock(lock, "runNEXT")
    first.release()
    lock.write_text("{ not valid json", encoding="utf-8")
    second = acquire_ledger_lock(lock, "runAFTER")
    second.release()


def test_crashed_process_releases_directory_lock(tmp_path):
    # F1: the lock is OS-owned, so duration never matters. A second
    # process cannot take it while the holder lives (however long that
    # is — there is no TTL to expire), and the OS releases it when the
    # holder dies, so the next run proceeds after a crash.
    import subprocess
    import sys as _sys

    import pytest as _pytest

    lock = ledger_lock_path(tmp_path)
    backend = Path(__file__).resolve().parents[1]
    child = (
        "import os, sys, time; "
        "sys.path.insert(0, sys.argv[1]); "
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings'); "
        "import django; django.setup(); "
        "from audits.realworld import acquire_ledger_lock; "
        "lock = acquire_ledger_lock(sys.argv[2], 'runCHILD'); "
        "print('ready', flush=True); "
        "time.sleep(60)"
    )
    proc = subprocess.Popen(
        [_sys.executable, "-c", child, str(backend), str(lock)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        line = proc.stdout.readline().strip()
        if line != "ready":
            proc.wait(timeout=60)
            _pytest.fail(f"lock-holder child failed to start: {line!r}")
        # Holder is alive: a concurrent start refuses, whenever it tries.
        with _pytest.raises(BudgetRefused):
            acquire_ledger_lock(lock, "runPARENT")
        # Simulate a crash: hard-kill the holder without any cleanup.
        proc.kill()
        proc.wait(timeout=60)
        # The OS releases the lock on process death: recovery proceeds.
        # Handle teardown can lag process exit slightly on Windows, so
        # poll briefly instead of assuming instant release.
        import time as _time

        deadline = _time.time() + 30
        while True:
            try:
                follow = acquire_ledger_lock(lock, "runPARENT")
                break
            except BudgetRefused:
                if _time.time() >= deadline:
                    raise
                _time.sleep(0.5)
        follow.release()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=60)


def test_custom_filename_ledger_blocks_default_restart(tmp_path, monkeypatch):
    # F2: discovery is content-based, not filename-based. A ledger with
    # outstanding reservations under a custom filename blocks a default
    # restart even
    # under a custom filename, before any provider call. Well-formed
    # non-ledger JSONL and empty files are ignored.
    manifest = _harness_manifest(tmp_path)
    fingerprint = dataset_fingerprint(manifest)
    ledgers = tmp_path / "results"
    ledgers.mkdir()
    (ledgers / "notes.jsonl").write_text('{"note": "not a ledger"}\n', encoding="utf-8")
    (ledgers / "empty.jsonl").write_text("\n", encoding="utf-8")
    custom = ledgers / "friday-run-notes.jsonl"
    handle = LedgerFile(custom)
    budget = EvalBudget(
        authorized=True,
        max_spend_usd="5.00",
        max_attempts_total=12,
        reserve_usd_per_call="2.00",
        ledger_file=handle,
        dataset_id=fingerprint,
    )
    budget.reserve(
        "Muse case-a baseline",
        max_attempts=3,
        provider="fake-muse",
        case_id="case-a",
        mode="baseline",
    )
    handle.close()
    hits = scan_ledger_dir(ledgers)
    assert len(hits) == 1
    assert hits[0][0].name == "friday-run-notes.jsonl"
    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    import pytest as _pytest

    with _pytest.raises(SystemExit, match="outstanding"):
        runner.run_realworld_live(
            manifest,
            tmp_path,
            10.0,
            True,
            muse_adapter=fake_muse,
            sam_adapter=fake_sam,
            results_dir=tmp_path / "results",
        )
    assert fake_muse.calls == []
    assert fake_sam.calls == []


def test_custom_filename_torn_ledger_fails_closed(tmp_path):
    # F2: a custom-named file carrying ledger identity but torn content
    # fails closed instead of being skipped as "unrelated".
    _harness_manifest(tmp_path)
    ledgers = tmp_path / "results"
    ledgers.mkdir()
    import json as _json

    import pytest as _pytest

    first = {"event": "reserved", "token": "tok1", "run_id": "runX"}
    (ledgers / "renamed.jsonl").write_text(
        _json.dumps(first) + "\n{ torn content\n", encoding="utf-8"
    )
    with _pytest.raises(BudgetRefused):
        scan_ledger_dir(ledgers)


def test_post_lock_scan_catches_race_ledger(tmp_path, monkeypatch):
    # P1 race: the authoritative scan runs under the held directory lock.
    # A reservation landing after any earlier check but before lock
    # acquisition is still caught, and no provider call fires.
    manifest = _harness_manifest(tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    runner = _load_eval_runner()
    real_acquire = runner.acquire_ledger_lock

    def raced_acquire(path, run_id):
        # Simulate the other process winning the race: its outstanding
        # reservation lands just before we hold the lock.
        _crashed_ledger_for(manifest, results, run_id="runRACER")
        return real_acquire(path, run_id)

    monkeypatch.setattr(runner, "acquire_ledger_lock", raced_acquire)
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    import pytest as _pytest

    with _pytest.raises(SystemExit, match="outstanding"):
        runner.run_realworld_live(
            manifest,
            tmp_path,
            10.0,
            True,
            muse_adapter=fake_muse,
            sam_adapter=fake_sam,
            results_dir=results,
        )
    assert fake_muse.calls == []
    assert fake_sam.calls == []


def test_settled_race_ledger_does_not_block(tmp_path, monkeypatch):
    # Control: a reservation that lands mid-startup but is fully settled
    # before our post-lock scan must not block execution.
    manifest = _harness_manifest(tmp_path)
    fingerprint = dataset_fingerprint(manifest)
    results = tmp_path / "results"
    results.mkdir()
    runner = _load_eval_runner()
    real_acquire = runner.acquire_ledger_lock

    def settled_acquire(path, run_id):
        settled = results / "2026-01-01-realworld-live-runSETTLED.jsonl"
        handle = LedgerFile(settled)
        budget = EvalBudget(
            authorized=True,
            max_spend_usd="5.00",
            max_attempts_total=12,
            reserve_usd_per_call="2.00",
            ledger_file=handle,
            dataset_id=fingerprint,
        )
        token = budget.reserve(
            "Muse case-a baseline",
            max_attempts=3,
            provider="fake-muse",
            case_id="case-a",
            mode="baseline",
        )
        budget.settle(token, attempts=1, cost_usd="0.01", provider="fake-muse")
        handle.close()
        return real_acquire(path, run_id)

    monkeypatch.setattr(runner, "acquire_ledger_lock", settled_acquire)
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    report = runner.run_realworld_live(
        manifest,
        tmp_path,
        10.0,
        True,
        muse_adapter=fake_muse,
        sam_adapter=fake_sam,
        results_dir=results,
    )
    assert fake_muse.calls != []
    assert report["spend_ledger"]["unknown_cost"] is False


def test_refused_run_releases_directory_lock(tmp_path, monkeypatch):
    # A run refused by the post-lock scan must release the lock, so the
    # next attempt can start (and report the blockers, not a held lock).
    manifest = _harness_manifest(tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    _crashed_ledger_for(manifest, results)
    import pytest as _pytest

    with _pytest.raises(SystemExit, match="outstanding"):
        _harness_run(tmp_path, manifest, monkeypatch)
    follow = acquire_ledger_lock(ledger_lock_path(results), "runNEXT")
    follow.release()


def test_added_case_does_not_evade_guard(tmp_path, monkeypatch):
    # Defect 1: adding a case changes the fingerprint, but the crashed
    # run's outstanding reservation already left the provider account,
    # so the extended run must still refuse before any provider call.
    import copy as _copy

    import pytest as _pytest

    manifest = _harness_manifest(tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    _crashed_ledger_for(manifest, results, run_id="runOLD")
    extended = _copy.deepcopy(manifest)
    extra = _copy.deepcopy(manifest["cases"][1])
    extra["id"] = "case-c"
    extended["cases"].append(extra)
    assert dataset_fingerprint(extended) != dataset_fingerprint(manifest)
    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    with _pytest.raises(SystemExit, match="outstanding"):
        runner.run_realworld_live(
            extended,
            tmp_path,
            15.0,
            True,
            muse_adapter=fake_muse,
            sam_adapter=fake_sam,
            results_dir=results,
        )
    assert fake_muse.calls == []
    assert fake_sam.calls == []


def test_directory_lock_blocks_different_datasets(tmp_path):
    # Defect 1: the directory-wide lock refuses concurrent runs even for
    # different datasets, closing the cross-dataset concurrency gap.
    import pytest as _pytest

    first = acquire_ledger_lock(ledger_lock_path(tmp_path), "runA")
    try:
        with _pytest.raises(BudgetRefused):
            acquire_ledger_lock(ledger_lock_path(tmp_path), "runB")
    finally:
        first.release()


def test_tokenless_records_stay_outstanding():
    # Defect 2: a settled record without a usable token must never cancel
    # a reservation; a reserved record without one is always unresolved.
    assert (
        find_outstanding(
            [
                {"event": "reserved", "label": "x"},
                {"event": "settled", "label": "x"},
            ]
        )
        != []
    )
    assert (
        find_outstanding(
            [
                {"event": "reserved", "token": None, "label": "x"},
                {"event": "settled", "token": None, "label": "x"},
            ]
        )
        != []
    )
    assert (
        find_outstanding(
            [
                {"event": "reserved", "token": 123, "label": "x"},
                {"event": "settled", "token": 123, "label": "x"},
            ]
        )
        != []
    )
    assert (
        find_outstanding(
            [
                {"event": "reserved", "token": "", "label": "x"},
                {"event": "settled", "token": "", "label": "x"},
            ]
        )
        != []
    )


def test_nondict_json_fails_closed_both_names(tmp_path):
    # Defect 3 (+review note 2): a ledger line that is valid JSON but not
    # an object fails closed, whether the file uses the conventional name
    # or a custom one.
    import pytest as _pytest

    import audits.realworld as _realworld

    for sub, name in (
        ("conv", "2026-01-01-realworld-live-runARR.jsonl"),
        ("custom", "renamed-array.jsonl"),
    ):
        ledgers = tmp_path / sub
        ledgers.mkdir()
        (ledgers / name).write_text("[1,2]\n", encoding="utf-8")
        with _pytest.raises(BudgetRefused):
            _realworld.scan_ledger_dir(ledgers)


def test_nondict_later_line_releases_lock_no_calls(tmp_path, monkeypatch):
    # Defect 3: a torn later line (valid JSON, not an object) refuses with
    # BudgetRefused — releasing the lock — before any provider call.
    import json as _json

    import pytest as _pytest

    manifest = _harness_manifest(tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    first = {
        "event": "reserved",
        "token": "tok1",
        "run_id": "runX",
        "label": "Muse case-a baseline",
        "dataset_id": dataset_fingerprint(manifest),
    }
    (results / "custom-torn.jsonl").write_text(
        _json.dumps(first) + "\n[1,2]\n", encoding="utf-8"
    )
    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    with _pytest.raises(SystemExit, match="malformed|unreadable"):
        runner.run_realworld_live(
            manifest,
            tmp_path,
            10.0,
            True,
            muse_adapter=fake_muse,
            sam_adapter=fake_sam,
            results_dir=results,
        )
    assert fake_muse.calls == []
    assert fake_sam.calls == []
    follow = acquire_ledger_lock(ledger_lock_path(results), "runNEXT")
    follow.release()


def test_ledger_creation_failure_releases_lock(tmp_path, monkeypatch):
    # Defect 4: if LedgerFile creation fails after lock acquisition, the
    # lock is still released and an in-process retry can acquire it.
    import pytest as _pytest

    manifest = _harness_manifest(tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    runner = _load_eval_runner()

    def _boom(path):
        raise OSError("disk full")

    monkeypatch.setattr(runner, "LedgerFile", _boom)
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    with _pytest.raises(OSError, match="disk full"):
        runner.run_realworld_live(
            manifest,
            tmp_path,
            10.0,
            True,
            muse_adapter=fake_muse,
            sam_adapter=fake_sam,
            results_dir=results,
        )
    assert fake_muse.calls == []
    assert fake_sam.calls == []
    follow = acquire_ledger_lock(ledger_lock_path(results), "runNEXT")
    follow.release()


def test_close_failure_still_releases_lock(tmp_path, monkeypatch):
    # Defect 4: a ledger_file.close() failure must not skip the release.
    import pytest as _pytest

    manifest = _harness_manifest(tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    runner = _load_eval_runner()
    _real_ledger_file = runner.LedgerFile

    class _ExplodingClose(_real_ledger_file):
        def close(self):
            raise OSError("fsync failed")

    monkeypatch.setattr(runner, "LedgerFile", _ExplodingClose)
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    with _pytest.raises(OSError, match="fsync failed"):
        runner.run_realworld_live(
            manifest,
            tmp_path,
            10.0,
            True,
            muse_adapter=fake_muse,
            sam_adapter=fake_sam,
            results_dir=results,
        )
    follow = acquire_ledger_lock(ledger_lock_path(results), "runNEXT")
    follow.release()


def test_ledger_creation_fsyncs_parent_dir(tmp_path, monkeypatch):
    # Defect 5: creating a new ledger fsyncs its parent directory on
    # POSIX (best-effort crash consistency); skipped on Windows, where
    # directory fsync is not supported.
    import os as _os

    import pytest as _pytest

    import audits.realworld as _realworld

    if _os.name == "nt":
        _pytest.skip("POSIX-only directory fsync")
    calls = []
    monkeypatch.setattr(
        _realworld, "_fsync_parent", lambda directory: calls.append(directory)
    )
    handle = _realworld.LedgerFile(tmp_path / "new-ledger.jsonl")
    try:
        assert calls == [tmp_path]
    finally:
        handle.close()
    # Reopening an existing ledger does not fsync the directory again.
    handle2 = _realworld.LedgerFile(tmp_path / "new-ledger.jsonl")
    try:
        assert calls == [tmp_path]
    finally:
        handle2.close()


def test_ledger_creation_skips_dir_fsync_on_windows(tmp_path, monkeypatch):
    # Defect 5: on Windows the directory fsync is skipped with a comment.
    import os as _os

    import pytest as _pytest

    import audits.realworld as _realworld

    if _os.name != "nt":
        _pytest.skip("Windows-only directory-fsync skip")
    calls = []
    monkeypatch.setattr(
        _realworld, "_fsync_parent", lambda directory: calls.append(directory)
    )
    handle = _realworld.LedgerFile(tmp_path / "new-ledger.jsonl")
    try:
        assert calls == []
    finally:
        handle.close()


def test_fsync_parent_fsyncs_directory(tmp_path, monkeypatch):
    # Defect 5 mechanics: the helper fsyncs exactly the directory fd.
    import os as _os

    import pytest as _pytest

    import audits.realworld as _realworld

    if _os.name == "nt":
        _pytest.skip("POSIX-only directory fsync")
    opened = {}
    _real_open = _os.open

    def _spy_open(path, flags, *args, **kwargs):
        fd = _real_open(path, flags, *args, **kwargs)
        opened.setdefault(str(path), []).append(fd)
        return fd

    seen = []
    monkeypatch.setattr(_os, "open", _spy_open)
    monkeypatch.setattr(_os, "fsync", lambda fd: seen.append(fd))
    _realworld._fsync_parent(tmp_path)
    # Exactly one fsync, targeting a descriptor opened for the directory.
    assert len(seen) == 1
    assert seen[0] in opened.get(str(tmp_path), [])


def test_lock_creation_fsyncs_on_posix(tmp_path, monkeypatch):
    # Defect 5: the lock file payload and its directory are fsync'd on
    # POSIX too; skipped on Windows.
    import os as _os

    import pytest as _pytest

    import audits.realworld as _realworld

    if _os.name == "nt":
        _pytest.skip("POSIX-only lock fsync")
    calls = []
    monkeypatch.setattr(
        _realworld, "_fsync_parent", lambda directory: calls.append(directory)
    )
    held = _realworld.acquire_ledger_lock(
        _realworld.ledger_lock_path(tmp_path), "runFSYNC"
    )
    try:
        assert calls == [tmp_path]
    finally:
        held.release()


def test_ledger_fsync_failure_closes_and_refuses(tmp_path, monkeypatch):
    # Issue 1: a parent-dir fsync failure must close the new handle and
    # refuse fail-closed (not leak the descriptor or raise raw OSError).
    # The supported branch is forced via the predicate so this runs
    # everywhere; real fsync mechanics stay covered by POSIX-only tests.
    import builtins as _builtins

    import pytest as _pytest

    import audits.realworld as _realworld

    monkeypatch.setattr(_realworld, "_should_fsync_parent", lambda: True)

    def _boom(directory):
        raise OSError("EINVAL")

    monkeypatch.setattr(_realworld, "_fsync_parent", _boom)
    real_open = _builtins.open
    handles = []

    def _spy_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(_builtins, "open", _spy_open)
    target = tmp_path / "new-ledger.jsonl"
    with _pytest.raises(BudgetRefused, match="durable creation"):
        _realworld.LedgerFile(target)
    assert handles != []
    assert all(handle.closed for handle in handles)
    # The empty file is left in place; it reads as no reservations.
    assert target.exists()
    assert target.stat().st_size == 0
    assert _realworld.read_ledger(target) == []


def test_ledger_open_failure_refuses(tmp_path, monkeypatch):
    # Issue 1: an open() failure surfaces as BudgetRefused, never a raw
    # OSError, so run.py has no raw exception path from ledger creation.
    import builtins as _builtins

    import pytest as _pytest

    import audits.realworld as _realworld

    def _boom(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(_builtins, "open", _boom)
    with _pytest.raises(BudgetRefused, match="cannot be opened"):
        _realworld.LedgerFile(tmp_path / "nope.jsonl")


def test_ledger_creation_calls_dir_fsync(tmp_path, monkeypatch):
    # Issue 1 wiring: creating a new ledger calls the directory fsync
    # exactly once (supported branch forced so this runs everywhere).
    import audits.realworld as _realworld

    monkeypatch.setattr(_realworld, "_should_fsync_parent", lambda: True)
    calls = []
    monkeypatch.setattr(
        _realworld, "_fsync_parent", lambda directory: calls.append(directory)
    )
    handle = _realworld.LedgerFile(tmp_path / "wired.jsonl")
    try:
        assert calls == [tmp_path]
    finally:
        handle.close()


def test_fsync_failure_end_to_end_refuses_cleanly(tmp_path, monkeypatch):
    # Issue 1 end-to-end: a dir-fsync failure during a live run refuses
    # via SystemExit "refusing: ..." (not "aborting ..."), fires zero
    # provider calls, and releases the directory lock.
    import pytest as _pytest

    import audits.realworld as _realworld

    monkeypatch.setattr(_realworld, "_should_fsync_parent", lambda: True)
    runner = _load_eval_runner()
    real_acquire = runner.acquire_ledger_lock
    armed = {"on": False}

    def _gated_acquire(path, run_id):
        lock = real_acquire(path, run_id)
        armed["on"] = True
        return lock

    def _gated_fsync(directory):
        if armed["on"]:
            raise OSError("EINVAL")

    monkeypatch.setattr(runner, "acquire_ledger_lock", _gated_acquire)
    monkeypatch.setattr(_realworld, "_fsync_parent", _gated_fsync)
    manifest = _harness_manifest(tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    with _pytest.raises(SystemExit, match="refusing"):
        runner.run_realworld_live(
            manifest,
            tmp_path,
            10.0,
            True,
            muse_adapter=fake_muse,
            sam_adapter=fake_sam,
            results_dir=results,
        )
    assert fake_muse.calls == []
    assert fake_sam.calls == []
    # Disarm the gated fsync: the follow-up acquisition must exercise the
    # real path (which fsyncs on POSIX), proving the run released the lock.
    armed["on"] = False
    follow = acquire_ledger_lock(ledger_lock_path(results), "runNEXT")
    follow.release()


def test_cli_never_selects_results_dir(tmp_path, monkeypatch):
    # Issue 2: the CLI exposes no results-dir option and passes none, so
    # production always guards the repo checkout's evals/results
    # directory and the boundary cannot be widened by accident later.
    import sys as _sys

    import pytest as _pytest

    runner = _load_eval_runner()
    seen = {}

    def _capture(*args, **kwargs):
        seen.update(kwargs)
        raise SystemExit("captured")

    monkeypatch.setattr(runner, "run_realworld_live", _capture)
    manifest = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "realworld-sample"
        / "manifest.json"
    )
    monkeypatch.setattr(
        _sys,
        "argv",
        [
            "run.py",
            "realworld-live",
            "--manifest",
            str(manifest),
            "--max-spend",
            "5.00",
            "--skip-sam",
        ],
    )
    with _pytest.raises(SystemExit, match="captured"):
        runner.main()
    assert "results_dir" not in seen
    # An explicit results-dir flag is rejected by the CLI parser.
    monkeypatch.setattr(
        _sys,
        "argv",
        [
            "run.py",
            "realworld-live",
            "--manifest",
            str(manifest),
            "--results-dir",
            str(tmp_path),
        ],
    )
    with _pytest.raises(SystemExit) as exc_info:
        runner.main()
    assert exc_info.value.code == 2


def test_accounting_history_immutable(tmp_path, monkeypatch):
    # Test G: refused or subsequent runs never modify existing events.
    manifest = _harness_manifest(tmp_path)
    ledgers = tmp_path / "results"
    ledgers.mkdir()
    crashed, _ = _crashed_ledger_for(manifest, ledgers, run_id="runFROZEN")
    before = crashed.read_bytes()
    import pytest as _pytest

    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    fake_sam = _HarnessSamAdapter()
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    with _pytest.raises(SystemExit):
        runner.run_realworld_live(
            manifest,
            tmp_path,
            10.0,
            True,
            muse_adapter=fake_muse,
            sam_adapter=fake_sam,
            results_dir=ledgers,
        )
    assert crashed.read_bytes() == before
    # A settled run appends only to its own file.
    settled = ledgers / "2026-01-01-realworld-live-runDONE.jsonl"
    handle = LedgerFile(settled)
    budget = EvalBudget(
        authorized=True,
        max_spend_usd="5.00",
        max_attempts_total=12,
        reserve_usd_per_call="2.00",
        ledger_file=handle,
        dataset_id=dataset_fingerprint(manifest),
    )
    token = budget.reserve("Muse case-a baseline", max_attempts=1)
    budget.settle(token, attempts=1, cost_usd="0.01", provider="fake-muse")
    handle.close()
    assert crashed.read_bytes() == before


# --- Item 5a: opt-in pattern (m4) harness mode ---------------------------


def _live_report(monkeypatch, tmp_path, pattern_mode, max_spend):
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    manifest = _harness_manifest(tmp_path)
    kwargs = {
        "muse_adapter": fake_muse,
        "sam_adapter": _HarnessSamAdapter(),
        "results_dir": tmp_path / "results",
        "ledger_path": tmp_path / "ledgers" / "probe.jsonl",
    }
    # The default-mode test relies on the real default: only pass the
    # flag when explicitly enabling the pattern mode.
    if pattern_mode:
        kwargs["pattern_mode"] = True
    report = runner.run_realworld_live(
        manifest,
        tmp_path,
        max_spend,
        False,
        **kwargs,
    )
    return report, fake_muse


def test_live_default_modes_are_baseline_and_reference(tmp_path, monkeypatch):
    report, fake_muse = _live_report(monkeypatch, tmp_path, False, 100.0)
    modes = {row["mode"] for row in report["rows"]}
    assert modes == {"baseline", "reference"}
    versions = {request.prompt_version for request in fake_muse.calls}
    assert versions == {"m2", "m3"}
    assert report["spend_preflight_usd"]["worst_case_usd"] == "8.00"


def test_live_pattern_mode_adds_m4(tmp_path, monkeypatch):
    report, fake_muse = _live_report(monkeypatch, tmp_path, True, 100.0)
    modes = {row["mode"] for row in report["rows"]}
    assert modes == {"baseline", "reference", "pattern"}
    versions = {request.prompt_version for request in fake_muse.calls}
    assert "m4" in versions
    assert report["spend_preflight_usd"]["worst_case_usd"] == "12.00"
    pattern = report["pattern_mode"]
    assert pattern["prompt_version"] == "m4"
    assert pattern["paired_overlap_vs_baseline"] == pattern["n_scored"]
    assert pattern["sam_localization_accuracy"] is None


def test_live_over_bound_refuses_before_any_call(tmp_path, monkeypatch):
    monkeypatch.setenv("VCC_LIVE_SMOKE", "1")
    runner = _load_eval_runner()
    fake_muse = _HarnessMuseAdapter()
    manifest = _harness_manifest(tmp_path)
    with pytest.raises(SystemExit, match="exceeds --max-spend"):
        runner.run_realworld_live(
            manifest,
            tmp_path,
            1.0,
            False,
            muse_adapter=fake_muse,
            sam_adapter=_HarnessSamAdapter(),
            results_dir=tmp_path / "results",
            ledger_path=tmp_path / "ledgers" / "probe.jsonl",
            pattern_mode=True,
        )
    assert fake_muse.calls == []


def test_dryrun_pattern_mode_reports_not_measured(tmp_path):
    runner = _load_eval_runner()
    manifest = _harness_manifest(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    default = runner.run_realworld_dryrun(manifest_path, tmp_path)
    flagged = runner.run_realworld_dryrun(manifest_path, tmp_path, pattern_mode=True)
    assert flagged["rows"] == default["rows"]
    assert flagged["summary"] == default["summary"]
    assert flagged["pattern_mode"]["status"] == "not measured"
