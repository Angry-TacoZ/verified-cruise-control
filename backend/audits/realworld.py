"""Real-world ADAS evaluation dataset + paired scoring (PR #3, M3/M4).

Versioned manifests describe genuine-photograph cases; photographs
themselves are NEVER committed (see ``evals/realworld/README.md``).
Ground truth stays independent of model output: predicted results never
modify labels, and scoring is a separate deterministic comparison.

Cost bounds reuse the application's own enforced caps: the live
pre-flight bound is cases x modes x per-job ``spend_cap_usd`` (see
``estimate_spend``), so no invented per-token prices are needed.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

MANIFEST_VERSION = "realworld-v1"

FIVE_STATES = (
    "verified",
    "visually_supported",
    "contradicted",
    "insufficient_evidence",
    "not_visually_verifiable",
)

# Equipment truth is tri-state: present, absent, or genuinely unknown.
EQUIPMENT_VALUES = ("equipped", "not-equipped", "unknown")

# Interpretation of the visible object relative to the advertised claim.
INTERPRETATION_VALUES = ("supports", "generic", "insufficient", "unknown")

MODES = ("baseline", "reference")

# Keys that must never appear in a manifest: personal data, VINs, plates,
# or embedded image bytes. Manifests describe photos; they never contain them.
FORBIDDEN_KEYS = frozenset(
    {
        "vin",
        "plate",
        "license_plate",
        "owner",
        "name",
        "email",
        "address",
        "image_data",
        "photo_bytes",
        "image_bytes",
        "data_uri",
    }
)

REQUIRED_VEHICLE_KEYS = frozenset({"year", "make", "model", "trim", "market"})
REQUIRED_CLAIM_KEYS = frozenset({"advertised", "normalized_feature", "claim_id"})
REQUIRED_PHOTO_KEYS = frozenset({"path", "sha256", "provenance"})
REQUIRED_TRUTH_KEYS = frozenset(
    {
        "equipment",
        "visible_object",
        "interpretation",
        "expected_state",
        "verification_method",
        "ambiguity",
    }
)


def _no_forbidden(obj, where: str) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in FORBIDDEN_KEYS:
                raise ValueError(
                    f"{where}: forbidden key {key!r} (privacy/photo bytes)"
                )
            _no_forbidden(value, f"{where}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            _no_forbidden(value, f"{where}[{i}]")


def validate_case(case: dict) -> dict:
    """Validate one evaluation case. Returns the case unchanged."""
    if not isinstance(case, dict):
        raise TypeError("case must be an object")
    _no_forbidden(case, "case")
    for key in ("id", "vehicle", "claim", "photo", "ground_truth"):
        if key not in case:
            raise ValueError(f"case missing required key {key!r}")
    if not case["id"] or not isinstance(case["id"], str):
        raise ValueError("case id must be a non-empty string")

    vehicle = case["vehicle"]
    missing = REQUIRED_VEHICLE_KEYS - set(vehicle)
    if missing:
        raise ValueError(f"case {case['id']}: vehicle missing {sorted(missing)}")

    claim = case["claim"]
    missing = REQUIRED_CLAIM_KEYS - set(claim)
    if missing:
        raise ValueError(f"case {case['id']}: claim missing {sorted(missing)}")

    photo = case["photo"]
    missing = REQUIRED_PHOTO_KEYS - set(photo)
    if missing:
        raise ValueError(f"case {case['id']}: photo missing {sorted(missing)}")
    path = photo["path"]
    if (
        not isinstance(path, str)
        or not path
        or Path(path).is_absolute()
        or ".." in Path(path).parts
    ):
        raise ValueError(
            f"case {case['id']}: photo.path must be a relative in-dataset path"
        )
    sha = photo["sha256"]
    if not isinstance(sha, str) or len(sha) != 64:
        raise ValueError(
            f"case {case['id']}: photo.sha256 must be a 64-char hex digest"
        )
    try:
        int(sha, 16)
    except ValueError:
        raise ValueError(f"case {case['id']}: photo.sha256 must be hex") from None

    truth = case["ground_truth"]
    missing = REQUIRED_TRUTH_KEYS - set(truth)
    if missing:
        raise ValueError(f"case {case['id']}: ground_truth missing {sorted(missing)}")
    if truth["equipment"] not in EQUIPMENT_VALUES:
        raise ValueError(
            f"case {case['id']}: bad equipment value {truth['equipment']!r}"
        )
    if truth["interpretation"] not in INTERPRETATION_VALUES:
        raise ValueError(
            f"case {case['id']}: bad interpretation {truth['interpretation']!r}"
        )
    if truth["expected_state"] not in FIVE_STATES:
        raise ValueError(
            f"case {case['id']}: bad expected_state {truth['expected_state']!r}"
        )
    return case


def validate_manifest(manifest, photos_root=None) -> list:
    """Validate a manifest dict or path; verify photo bytes when a root is given."""
    if isinstance(manifest, (str, Path)):
        import json

        manifest = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise TypeError("manifest must be an object")
    _no_forbidden(manifest, "manifest")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError(
            f"unsupported manifest_version {manifest.get('manifest_version')!r}, "
            f"expected {MANIFEST_VERSION!r}"
        )
    if manifest.get("split") not in ("dev", "holdout"):
        raise ValueError("manifest split must be 'dev' or 'holdout'")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("manifest cases must be a non-empty list")
    seen = set()
    for case in cases:
        validate_case(case)
        if case["id"] in seen:
            raise ValueError(f"duplicate case id {case['id']!r}")
        seen.add(case["id"])
        if photos_root is not None:
            photo_path = Path(photos_root) / case["photo"]["path"]
            if not photo_path.is_file():
                raise ValueError(f"case {case['id']}: missing photo file {photo_path}")
            digest = hashlib.sha256(photo_path.read_bytes()).hexdigest()
            if digest != case["photo"]["sha256"]:
                raise ValueError(f"case {case['id']}: sha256 mismatch for {photo_path}")
    return cases


def score_case(case: dict, recorded: dict) -> dict:
    """Deterministically score one recorded model output against ground truth.

    Returns a row dict with per-stage correctness plus safety flags:
    ``hallucinated_positive`` (claimed support where truth says not-equipped)
    and ``unsupported_equipped`` (asserted a feature the photo cannot show).
    """
    case_id = case.get("id")
    mode = recorded.get("mode", "baseline")
    status = recorded.get("status", "scored")
    if status != "scored":
        return {"case_id": case_id, "mode": mode, "status": status}

    truth = case["ground_truth"]
    expected_state = truth["expected_state"]
    state = recorded.get("state")
    control = (recorded.get("control") or "").strip().lower()
    visible = (truth.get("visible_object") or "").strip().lower()
    interpretation = recorded.get("interpretation")
    sam_target = (recorded.get("sam_target") or "").strip().lower()

    control_correct = bool(control) and (
        control == visible or control in visible or visible in control
    )
    predicted_positive = state in ("verified", "visually_supported")
    truth_positive = expected_state in ("verified", "visually_supported")

    hallucinated_positive = (
        bool(predicted_positive) and truth["equipment"] == "not-equipped"
    )
    # Unsupported = a positive prediction the visible evidence cannot carry,
    # regardless of whether the vehicle is actually equipped. Hallucinated
    # is the stricter subset where the equipment is known to be absent.
    unsupported_equipped = bool(predicted_positive) and truth["interpretation"] in (
        "generic",
        "insufficient",
        "unknown",
    )
    sam_ok = (
        bool(sam_target)
        and bool(control)
        and (sam_target in control or control in sam_target)
    )

    return {
        "case_id": case_id,
        "mode": mode,
        "status": "scored",
        "state": state,
        "state_correct": state == expected_state,
        "control_correct": control_correct,
        "predicted_positive": predicted_positive,
        "truth_positive": truth_positive,
        "support_correct": bool(predicted_positive and truth_positive),
        "hallucinated_positive": hallucinated_positive,
        "unsupported_equipped": unsupported_equipped,
        "sam_target_ok": sam_ok,
        "interpretation_match": interpretation == truth["interpretation"],
        "recorded_prompt_version": recorded.get("prompt_version"),
    }


def _rate(numer: int, denom: int):
    return (numer / denom) if denom else None


def _mode_stats(rows: list) -> dict:
    scored = [r for r in rows if r.get("status") == "scored"]
    n = len(scored)
    hall = sum(1 for r in scored if r.get("hallucinated_positive"))
    unsup = sum(1 for r in scored if r.get("unsupported_equipped"))
    tp = sum(1 for r in scored if r.get("support_correct"))
    pred_pos = sum(1 for r in scored if r.get("predicted_positive"))
    truth_pos = sum(1 for r in scored if r.get("truth_positive"))
    return {
        "n": n,
        "control_correct": sum(1 for r in scored if r.get("control_correct")),
        "state_correct": sum(1 for r in scored if r.get("state_correct")),
        "support_correct": tp,
        "hallucinated_positive": hall,
        "unsupported_equipped": unsup,
        "predicted_positives": pred_pos,
        "truth_positives": truth_pos,
        "precision": _rate(tp, pred_pos),
        "recall": _rate(tp, truth_pos),
        "state_accuracy": _rate(sum(1 for r in scored if r.get("state_correct")), n),
        "control_accuracy": _rate(
            sum(1 for r in scored if r.get("control_correct")), n
        ),
        "hallucination_count": hall,
        "unsupported_count": unsup,
        "sam_target_ok": sum(1 for r in scored if r.get("sam_target_ok")),
        "missing_or_unreadable": sum(
            1 for r in rows if r.get("status") in ("missing-photo", "unreadable")
        ),
        "insufficient_unknown_rate": _rate(
            sum(
                1
                for r in scored
                if r.get("state")
                in ("insufficient_evidence", "not_visually_verifiable")
            ),
            n,
        ),
    }


def summarize(manifest_id: str, rows: list) -> dict:
    """Aggregate scored rows into per-mode stats plus paired-case overlap."""
    by_mode = {}
    for mode in MODES:
        by_mode[mode] = _mode_stats([r for r in rows if r.get("mode") == mode])
    base_ids = {
        r["case_id"]
        for r in rows
        if r.get("mode") == "baseline" and r.get("status") == "scored"
    }
    ref_ids = {
        r["case_id"]
        for r in rows
        if r.get("mode") == "reference" and r.get("status") == "scored"
    }
    return {
        "manifest": manifest_id,
        "n_rows": len(rows),
        "paired_cases": len(base_ids & ref_ids),
        "modes": by_mode,
    }


def estimate_spend(
    n_cases: int,
    modes: int = 2,
    muse_calls_per_case: int = 1,
    sam_calls_per_case: int = 1,
    spend_cap_usd: str = "2.00",
) -> dict:
    """Pre-flight bound reusing the app's own per-job spend cap.

    Default matches ``settings.AUDIT_SPEND_CAP_USD``; pass the configured
    value explicitly when the deployment overrides it.

    Worst case = every job burns its full ``spend_cap_usd``. No invented
    per-token prices; Decimal-safe like the application ledger.
    """
    cap = Decimal(spend_cap_usd)
    jobs = n_cases * modes
    return {
        "cases": n_cases,
        "modes": modes,
        "jobs": jobs,
        "muse_calls_planned": jobs * muse_calls_per_case,
        "sam_calls_planned": jobs * sam_calls_per_case,
        "worst_case_usd": str(cap * jobs),
        "spend_cap_usd_per_job": str(cap),
    }


def render_markdown(manifest_id: str, summary: dict, rows: list) -> str:
    """Render the human-readable paired comparison report."""
    lines = [
        f"# Real-world paired evaluation: {manifest_id}",
        "",
        f"Rows scored: {summary['n_rows']}; paired_cases={summary['paired_cases']}.",
        "Small samples are pilot results, not significance claims.",
        "",
        "| Metric | Baseline | Reference-assisted |",
        "|---|---|---|",
    ]
    base = summary["modes"]["baseline"]
    ref = summary["modes"]["reference"]

    def fmt(value, denom, kind="rate"):
        if value is None:
            return "not measured"
        if kind == "rate":
            return f"{value:.2f} ({denom})"
        return f"{value}/{denom}"

    pairs = [
        (
            "Control identification accuracy",
            _rate(base["control_correct"], base["n"]),
            base["n"],
            _rate(ref["control_correct"], ref["n"]),
            ref["n"],
            "rate",
        ),
        (
            "Final-state accuracy",
            _rate(base["state_correct"], base["n"]),
            base["n"],
            _rate(ref["state_correct"], ref["n"]),
            ref["n"],
            "rate",
        ),
        (
            "Feature-support precision",
            base["precision"],
            base["predicted_positives"],
            ref["precision"],
            ref["predicted_positives"],
            "rate",
        ),
        (
            "Feature-support recall",
            base["recall"],
            base["truth_positives"],
            ref["recall"],
            ref["truth_positives"],
            "rate",
        ),
        (
            "False visual-support count",
            base["hallucination_count"],
            base["n"],
            ref["hallucination_count"],
            ref["n"],
            "count",
        ),
        (
            "Unsupported inference count",
            base["unsupported_count"],
            base["n"],
            ref["unsupported_count"],
            ref["n"],
            "count",
        ),
        (
            "SAM target-ok count",
            base["sam_target_ok"],
            base["n"],
            ref["sam_target_ok"],
            ref["n"],
            "count",
        ),
        (
            "Insufficient/unknown rate",
            base["insufficient_unknown_rate"],
            base["n"],
            ref["insufficient_unknown_rate"],
            ref["n"],
            "rate",
        ),
    ]
    for name, bv, bd, rv, rd, kind in pairs:
        lines.append(f"| {name} | {fmt(bv, bd, kind)} | {fmt(rv, rd, kind)} |")
    lines += ["", "## Per-case rows", ""]
    for row in rows:
        lines.append(
            f"- {row.get('case_id')} [{row.get('mode')}]: status={row.get('status')}"
            + (
                f" state={row.get('state')} state_correct={row.get('state_correct')}"
                f" control_correct={row.get('control_correct')}"
                f" hallucinated={row.get('hallucinated_positive')}"
                f" unsupported={row.get('unsupported_equipped')}"
                if row.get("status") == "scored"
                else ""
            )
        )
    lines.append("")
    return "\n".join(lines)
