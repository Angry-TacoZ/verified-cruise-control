"""PR #2/#3 eval runner: reconcile goldens (free) and live Muse quality (gated).

Usage:
    uv run python evals/run.py reconcile            # zero inference, always safe
    uv run python evals/run.py muse-live            # 1 live call, needs approval
    uv run python evals/run.py realworld-dryrun --manifest <path>
                                                    # zero inference, always safe
    uv run python evals/run.py realworld-live --manifest <path>
                                                    # paired live eval, needs approval
    uv run python evals/run.py realworld-validate --manifest <path>
                                                    # zero inference, always safe
    uv run python evals/run.py realworld-freeze --manifest <holdout> \
        --other-manifest <dev> --reviewer-role <role>
                                                    # zero inference, always safe

``muse-live`` refuses to run unless ``VCC_LIVE_SMOKE=1`` is set, a Meta
(Muse) key is present, and the estimated spend is under ``--max-spend``
(default $0.25). Never run it in CI.

``realworld-dryrun`` validates a realworld-v1 manifest, verifies photo
bytes when present, scores recorded model outputs when given, and prints
the spend preflight. ``realworld-validate`` runs the offline intake
gates (schema, value-level privacy scan, SHA-256 verification,
photo-only label constraints, linked-register admission, cross-split
and duplicate checks, holdout freeze check, quality reports) with no
provider calls. ``realworld-live`` runs baseline (m2) vs
reference-assisted (m3) Muse observations per case plus deterministic
reconciliation; SAM localization runs only with ``VCC_LIVE_SAM=1`` and a
Meta key, otherwise the SAM stage is reported as not measured. Never run
live evals in CI. Every call is pre-reserved to a write-ahead fsync'd
ledger (``--ledger`` reuses a clean trail; outstanding reservations
refuse).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from audits.models import Observation
from audits.pipeline import (
    MUSE_MAX_OUTPUT_TOKENS,
    PATTERN_PROMPT_VERSION,
    PROMPT_VERSION,
    REFERENCE_PROMPT_VERSION,
    SYSTEM_INSTRUCTIONS,
    build_reference_block,
    build_system_instructions,
    build_user_text,
    reconcile_claim,
)
from audits.realworld import (
    BudgetRefused,
    EvalBudget,
    LedgerFile,
    acquire_ledger_lock,
    assert_ledger_clear,
    build_eval_observation,
    build_sam_eval_observation,
    dataset_fingerprint,
    estimate_spend,
    fresh_ledger_path,
    ledger_lock_path,
    muse_verdict,
    refuse_if_blocked,
    render_markdown,
    scan_ledger_dir,
    score_case,
    select_sam_target,
    summarize,
    validate_manifest,
)
from audits.realworld_intake import (
    format_validate_output,
    freeze_from_paths,
    validate_intake,
    validate_register,
)
from django.conf import settings

CASES = REPO / "evals" / "cases" / "v1"
RESULTS = REPO / "evals" / "results"
FIXTURES = REPO / "fixtures" / "synthetic-listings" / "photos"
MIME = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


GOLDENS_VERSION = "v4"


def run_reconcile(version: str = GOLDENS_VERSION) -> dict:
    path = REPO / "evals" / "cases" / version / "reconcile_goldens.json"
    goldens = json.loads(path.read_text())
    photo_ids = [uuid.uuid4() for _ in range(4)]
    scored = []
    for case in goldens["cases"]:
        rows = [
            Observation(
                source=item["source"],
                verdict=item["verdict"],
                photo_id=photo_ids[item["photo"]],
                extra=item.get("extra", {}),
            )
            for item in case["observations"]
        ]
        state, summary = reconcile_claim(
            rows, case.get("claim", ""), case.get("vehicle")
        )
        scored.append(
            {
                "id": case["id"],
                "expected": case["expected"],
                "got": state,
                "summary": summary,
                "pass": state == case["expected"],
            }
        )
    passed = sum(1 for item in scored if item["pass"])
    return {
        "eval": "reconcile",
        "rule": goldens["rule"],
        "cases_version": goldens["version"],
        "passed": passed,
        "total": len(scored),
        "cases": scored,
    }


def run_muse_live(max_spend: float) -> dict:
    from providers.base import ImageAttachment
    from providers.meta_muse import MetaMuseAdapter, MuseRequest

    if os.environ.get("VCC_LIVE_SMOKE") != "1":
        raise SystemExit("refusing: set VCC_LIVE_SMOKE=1 to run live evals")
    truth = json.loads((CASES / "muse_fixture_ground_truth.json").read_text())
    # Stable claim IDs derived positionally (c1..cN); the v1 case file is
    # never edited to fit the harness (Rule D).
    entries = [
        {"id": f"c{i + 1}", "text": text} for i, text in enumerate(truth["claims"])
    ]
    acceptable = set(truth["acceptable_verdicts"])
    attachments = []
    for name in truth["photos"]:
        data = (FIXTURES / name).read_bytes()
        attachments.append(
            ImageAttachment(photo_id=name, mime_type=MIME[Path(name).suffix], data=data)
        )
    adapter = MetaMuseAdapter()
    adapter.require_configured()
    request = MuseRequest(
        prompt_version=PROMPT_VERSION,
        system_instructions=SYSTEM_INSTRUCTIONS,
        user_text=build_user_text(entries, truth["photos"]),
        photo_ids=tuple(truth["photos"]),
        images=tuple(attachments),
        # Six (claim, photo) observations with descriptions do not fit the
        # 2048-token default; the first live attempt truncated and was
        # correctly quarantined as malformed.
        max_output_tokens=4096,
    )
    result = adapter.analyze(request)
    usage = result.usage
    cost = usage.estimated_cost_usd
    if cost is None or cost > max_spend:
        raise SystemExit(f"refusing: live cost {cost} exceeds bound ${max_spend}")
    id_to_text = {entry["id"]: entry["text"] for entry in entries}
    scored = []
    for item in result.observations:
        if item["ambiguous"]:
            verdict = "unclear"
        elif str(item["visible_object"]).strip().lower() == "none":
            verdict = "absent"
        else:
            verdict = "present"
        cid = item["claim_concept"]
        scored.append(
            {
                "photo_id": item["photo_id"],
                "claim_id": cid,
                "claim": id_to_text.get(cid, f"UNKNOWN {cid}"),
                "verdict": verdict,
                "pass": verdict in acceptable and cid in id_to_text,
                "description": item["description"],
            }
        )
    present = sum(1 for item in scored if item["verdict"] == "present")
    return {
        "eval": "muse-live",
        "prompt_version": PROMPT_VERSION,
        "model": result.model,
        "cases_version": truth["version"],
        "unsupported_inference_rate": present / len(scored) if scored else 1.0,
        "passed": sum(1 for item in scored if item["pass"]),
        "total": len(scored),
        "cost_usd": cost,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "latency_ms": usage.latency_ms,
            "pricing_version": usage.pricing_version,
        },
        "cases": scored,
    }


def _resolve_photos_root(manifest_path: Path, explicit: str | None) -> Path:
    # Manifest photo paths are relative to the manifest's own directory
    # (e.g. "photos/foo.jpg" beside the manifest); the override covers
    # layouts where bytes live elsewhere.
    if explicit:
        return Path(explicit)
    return manifest_path.parent


def run_realworld_dryrun(
    manifest_path: Path,
    photos_root: Path,
    recorded_path=None,
    pattern_mode: bool = False,
) -> dict:
    """Zero-inference paired-eval rehearsal: validate, verify, score, bound.

    With ``pattern_mode`` the report additionally states the m4 pattern
    mode as "not measured" (the dry-run path has no recorded m4 vectors
    and never fabricates rows); the default run is unchanged.
    """
    cases = validate_manifest(manifest_path, photos_root=photos_root)
    rows = []
    if recorded_path is not None:
        recorded = json.loads(Path(recorded_path).read_text(encoding="utf-8"))
        by_id = {case["id"]: case for case in cases}
        for entry in recorded:
            case = by_id.get(entry.get("case_id"))
            if case is None:
                raise SystemExit(
                    f"recorded output for unknown case {entry.get('case_id')!r}"
                )
            rows.append(score_case(case, entry))
    summary = summarize(manifest_path.stem, rows)
    preflight = estimate_spend(
        len(cases),
        modes=2,
        muse_calls_per_case=1,
        sam_calls_per_case=1,
        spend_cap_usd=str(settings.AUDIT_SPEND_CAP_USD),
    )
    report = {
        "eval": "realworld-dryrun",
        "manifest": manifest_path.name,
        "manifest_version": "realworld-v1",
        "cases": len(cases),
        "case_ids": [case["id"] for case in cases],
        "summary": summary,
        "rows": rows,
        "spend_preflight_usd": preflight,
        "markdown": render_markdown(manifest_path.stem, summary, rows),
        "live_inference": False,
        "cost_usd": 0,
    }
    if pattern_mode:
        report["pattern_mode"] = {
            "status": "not measured",
            "reason": "no recorded m4 vectors",
            "prompt_version": PATTERN_PROMPT_VERSION,
        }
    return report


def run_intake_validate(
    manifest_path: Path,
    photos_root: Path,
    *,
    other_manifest=None,
    other_photos_root=None,
    register_path=None,
    enforce_freeze: bool = False,
    dispositions_path=None,
) -> dict:
    """Zero-inference intake validation: schema, privacy, hashes, gates.

    Never calls providers and reads no provider credentials. Links the
    private register (by matching entry id to case id) only when
    ``--register`` points at one; unmatched register entries are
    reported, and the public branch of the report stays counts-only.
    """
    entries = None
    if register_path is not None:
        register = json.loads(Path(register_path).read_text(encoding="utf-8"))
        validate_register(register)
        entries = {entry["id"]: entry for entry in register["entries"]}
    dispositions = None
    if dispositions_path is not None:
        dispositions = json.loads(Path(dispositions_path).read_text(encoding="utf-8"))
    return validate_intake(
        manifest=manifest_path,
        photos_root=photos_root,
        other_manifest=other_manifest,
        other_photos_root=(
            other_photos_root
            or (Path(other_manifest).parent if other_manifest else None)
        ),
        entries=entries,
        enforce_freeze=enforce_freeze,
        dispositions=dispositions,
    )


def run_intake_freeze(
    manifest_path: Path,
    photos_root: Path,
    *,
    dev_manifest_path,
    other_photos_root=None,
    reviewer_role: str,
    dispositions_path=None,
    replace_reason=None,
) -> dict:
    """Zero-inference holdout freeze. Never calls providers.

    Photo paths derive from the photos roots. The freeze refuses
    without a dev manifest, full photo coverage, matching digests, a
    holdout split, and (on re-freeze) a recorded replace reason.
    """
    dispositions = None
    if dispositions_path is not None:
        dispositions = json.loads(Path(dispositions_path).read_text(encoding="utf-8"))
    return freeze_from_paths(
        manifest_path=manifest_path,
        dev_manifest_path=dev_manifest_path,
        photos_root=photos_root,
        other_photos_root=other_photos_root or Path(dev_manifest_path).parent,
        reviewer_role=reviewer_role,
        dispositions=dispositions,
        replace_reason=replace_reason,
    )


def _refuse(message: str) -> None:
    raise SystemExit(f"refusing: {message}")


def _pattern_block(rows: list) -> dict:
    """Per-mode denominators for the opt-in pattern (m4) mode.

    Reported with its own denominators and paired overlap vs baseline;
    SAM localization stays "not measured" without independent labels.
    """
    pattern_rows = [r for r in rows if r.get("mode") == "pattern"]
    scored = [r for r in pattern_rows if r.get("status") == "scored"]
    base_ids = {
        r["case_id"]
        for r in rows
        if r.get("mode") == "baseline" and r.get("status") == "scored"
    }
    pattern_ids = {r["case_id"] for r in scored}
    return {
        "prompt_version": PATTERN_PROMPT_VERSION,
        "n": len(pattern_rows),
        "n_scored": len(scored),
        "support_correct": sum(1 for r in scored if r.get("support_correct")),
        "paired_overlap_vs_baseline": len(pattern_ids & base_ids),
        "sam_localization_accuracy": None,
    }


def run_realworld_live(
    manifest_path: Path,
    photos_root: Path,
    max_spend: float,
    include_sam: bool,
    *,
    muse_adapter=None,
    sam_adapter=None,
    results_dir=None,
    ledger_path=None,
    pattern_mode: bool = False,
) -> dict:
    """Paired baseline (m2) vs reference-assisted (m3) live evaluation.

    Injectable adapters/directories exist for deterministic offline tests
    (fake adapters, tmp dirs); production calls construct the real Meta
    Muse and Meta SAM adapters. No paid inference happens without VCC_LIVE_SMOKE=1
    (plus VCC_LIVE_SAM=1 for the SAM stage) and configured provider keys.

    With ``pattern_mode`` an opt-in third "pattern" mode (prompt m4)
    joins the loop; the default two-mode run is byte-identical without it.
    """
    from providers.base import ImageAttachment
    from providers.meta_muse import MetaMuseAdapter, MuseRequest

    if os.environ.get("VCC_LIVE_SMOKE") != "1":
        raise SystemExit("refusing: set VCC_LIVE_SMOKE=1 to run live evals")
    cases = validate_manifest(manifest_path, photos_root=photos_root)
    if isinstance(manifest_path, (str, Path)):
        manifest_id = Path(manifest_path).stem
        manifest_name = Path(manifest_path).name
    else:
        manifest_id = str(manifest_path.get("dataset", "manifest"))
        manifest_name = manifest_id
    if include_sam and sam_adapter is None:
        if os.environ.get("VCC_LIVE_SAM") != "1":
            raise SystemExit(
                "refusing: SAM stage needs VCC_LIVE_SAM=1 (else rerun with --skip-sam)"
            )
        from providers.meta import MetaSamAdapter, SamRequest

        sam_adapter = MetaSamAdapter()
    if sam_adapter is not None:
        sam_adapter.require_configured()
    modes = (
        ("baseline", "reference", "pattern")
        if pattern_mode
        else ("baseline", "reference")
    )
    preflight = estimate_spend(
        len(cases),
        modes=len(modes),
        muse_calls_per_case=1,
        sam_calls_per_case=1 if include_sam else 0,
        spend_cap_usd=str(settings.AUDIT_SPEND_CAP_USD),
    )
    if float(preflight["worst_case_usd"]) > max_spend:
        raise SystemExit(
            f"refusing: worst-case bound ${preflight['worst_case_usd']} exceeds --max-spend ${max_spend}"
        )
    if muse_adapter is None:
        muse_adapter = MetaMuseAdapter()
    muse_adapter.require_configured()

    out_dir = Path(results_dir) if results_dir is not None else RESULTS
    out_dir.mkdir(parents=True, exist_ok=True)

    # Cross-run billing safety: one directory-wide coordination lock (an
    # OS-backed exclusive lock held open for the whole run — no TTL, so a
    # live evaluation can never lose it by running long; the OS releases
    # it if the holder process dies), then — while holding that lock — an
    # authoritative content-based scan of the ledger directory for ANY
    # unresolved reservation from previous runs, regardless of dataset:
    # spend already left the provider account, so a renamed manifest, an
    # added case, or a different dataset cannot evade the guard. Any hit
    # refuses before adapters are even constructed; only the scan under
    # the held lock authorizes execution, so a reservation landing
    # between scan and lock acquisition cannot slip through. An explicit
    # --ledger path is additionally checked in full. Boundary: this
    # guards the evaluations sharing the managed results directory only.
    dataset_id = dataset_fingerprint(manifest_path)
    per_call = str(settings.AUDIT_SPEND_CAP_USD)
    run_id = uuid.uuid4().hex[:12]
    # Lock first: scanning before acquiring leaves a check-then-act race
    # where another process persists an outstanding reservation after our
    # scan but before our lock, and we would proceed on stale accounting.
    lock_path = ledger_lock_path(out_dir)
    try:
        dataset_lock = acquire_ledger_lock(lock_path, run_id)
    except BudgetRefused as exc:
        _refuse(str(exc))
    # Everything below runs under the held lock; the finally releases it
    # on every path, and a close() failure cannot skip the release.
    ledger_file = None
    try:
        try:
            refuse_if_blocked(scan_ledger_dir(out_dir))
        except BudgetRefused as exc:
            _refuse(str(exc))
        if ledger_path is None:
            ledger_path = fresh_ledger_path(out_dir, "realworld-live", run_id)
        else:
            ledger_path = Path(ledger_path)
            if ledger_path.exists():
                try:
                    assert_ledger_clear(ledger_path)
                except BudgetRefused as exc:
                    _refuse(str(exc))
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        # A ledger that cannot be opened or confirmed durable refuses
        # cleanly (as SystemExit "refusing: ..."), like every other
        # pre-inference gate, instead of escaping as a raw exception.
        try:
            ledger_file = LedgerFile(ledger_path)
        except BudgetRefused as exc:
            _refuse(str(exc))
        budget = EvalBudget(
            # Authorization verified above (VCC_LIVE_SMOKE + configured keys).
            authorized=True,
            max_spend_usd=str(max_spend),
            max_attempts_total=preflight["muse_calls_planned"] * 3
            + preflight["sam_calls_planned"] * 4,
            reserve_usd_per_call=per_call,
            ledger_file=ledger_file,
            run_id=run_id,
            dataset_id=dataset_id,
        )

        rows = []
        raw = []
        try:
            for case in cases:
                photo_rel = case["photo"]["path"]
                data = (photos_root / photo_rel).read_bytes()
                claim = case["claim"]
                entries = [{"id": claim["claim_id"], "text": claim["advertised"]}]
                photo_id = case["id"]
                attachment = ImageAttachment(
                    photo_id=photo_id,
                    mime_type=MIME[Path(photo_rel).suffix.lower()],
                    data=data,
                )
                for mode in modes:
                    if mode == "reference":
                        block = build_reference_block([claim["advertised"]])
                        if block:
                            system = build_system_instructions(block)
                            prompt_version = REFERENCE_PROMPT_VERSION
                        else:
                            # No RSA guidance maps to this claim: reference mode
                            # degrades to the baseline prompt; record the fallback.
                            system = SYSTEM_INSTRUCTIONS
                            prompt_version = PROMPT_VERSION
                    elif mode == "pattern":
                        system = build_system_instructions("", pattern_guidance=True)
                        prompt_version = PATTERN_PROMPT_VERSION
                    else:
                        system = SYSTEM_INSTRUCTIONS
                        prompt_version = PROMPT_VERSION
                    request = MuseRequest(
                        prompt_version=prompt_version,
                        system_instructions=system,
                        user_text=build_user_text(entries, [photo_id]),
                        photo_ids=(photo_id,),
                        images=(attachment,),
                        max_output_tokens=MUSE_MAX_OUTPUT_TOKENS,
                    )
                    try:
                        result = budget.guarded(
                            f"Muse {case['id']} {mode}",
                            muse_adapter,
                            request,
                            max_attempts=3,
                            case_id=case["id"],
                            mode=mode,
                        )
                    except BudgetRefused as exc:
                        _refuse(str(exc))
                    obs_list = list(result.observations or [])
                    if not obs_list:
                        # Empty model output is not evidence: record the gap,
                        # never reconcile a fabricated observation.
                        recorded = {
                            "case_id": case["id"],
                            "mode": mode,
                            "status": "missing-observation",
                            "prompt_version": prompt_version,
                        }
                        rows.append(score_case(case, recorded))
                        raw.append(
                            {
                                "case_id": case["id"],
                                "mode": mode,
                                "prompt_version": prompt_version,
                                "muse_observation": None,
                                "sam_status": "skipped",
                                "sam_masks": 0,
                                "state": None,
                            }
                        )
                        continue
                    obs = obs_list[0]
                    verdict = muse_verdict(obs)
                    visible = str(obs.get("visible_object", "") or "")
                    # F2: reconcile the complete validated observation —
                    # symbols, locations, dedicated flags, nested controls —
                    # not a visible_object-only projection.
                    eval_obs = build_eval_observation(
                        obs, claim_id=claim["claim_id"], photo_id=photo_id
                    )
                    # F2: SAM target comes from the production selector over
                    # the full observation (nested controls included), never
                    # from the top-level object alone. No valid target means
                    # no SAM call, explicitly recorded.
                    sam_target = ""
                    target_source = None
                    control_index = None
                    recon_inputs = [eval_obs]
                    sam_masks = 0
                    if not include_sam:
                        sam_status = "skipped"
                    else:
                        selection = select_sam_target(
                            eval_obs, claim["advertised"], case["vehicle"]
                        )
                        if selection is None:
                            sam_status = "unavailable-target"
                        else:
                            sam_target, target_source, control_index = selection
                            from PIL import Image as PILImage

                            with PILImage.open(photos_root / photo_rel) as img:
                                width, height = img.size
                            sam_request = SamRequest(
                                photo=attachment,
                                image_width=width,
                                image_height=height,
                                concept=sam_target,
                            )
                            try:
                                sam_result = budget.guarded(
                                    f"SAM {case['id']} {mode}",
                                    sam_adapter,
                                    sam_request,
                                    max_attempts=4,
                                    case_id=case["id"],
                                    mode=mode,
                                )
                            except BudgetRefused as exc:
                                _refuse(str(exc))
                            sam_masks = len(sam_result.records or [])
                            sam_status = "localized" if sam_masks else "no-mask"
                            recon_inputs.append(
                                build_sam_eval_observation(
                                    eval_obs,
                                    concept=sam_target,
                                    target_source=target_source,
                                    control_index=control_index,
                                    masks=sam_masks,
                                )
                            )
                    state, _summary = reconcile_claim(
                        recon_inputs,
                        claim["advertised"],
                        case["vehicle"],
                    )
                    recorded = {
                        "case_id": case["id"],
                        "mode": mode,
                        "status": "scored",
                        "state": state,
                        "prompt_version": prompt_version,
                        "control": visible,
                        "nested_control_count": len(obs.get("controls") or []),
                        "interpretation": "supports"
                        if state in ("verified", "visually_supported")
                        else ("unknown" if verdict == "unclear" else "insufficient"),
                        "sam_target": sam_target,
                        "sam_target_source": target_source,
                        "sam_control_index": control_index,
                        "sam_masks": sam_masks,
                        "sam_status": sam_status,
                    }
                    rows.append(score_case(case, recorded))
                    raw.append(
                        {
                            "case_id": case["id"],
                            "mode": mode,
                            "prompt_version": prompt_version,
                            "muse_observation": obs,
                            "sam_target": sam_target,
                            "sam_status": sam_status,
                            "sam_masks": sam_masks,
                            "state": state,
                        }
                    )
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure aborts, ledger stays durable
            raise SystemExit(f"aborting live eval after provider error: {exc}")
    finally:
        # Every event was already fsync'd at reservation/settlement time;
        # closing only flushes the file handle itself. The directory lock
        # is always released so a finished run never blocks the next one;
        # a crashed holder's lock is released by the OS on process death.
        # The nested finally keeps a close() failure from skipping the
        # release; a run that never created its ledger still releases it.
        if ledger_file is not None:
            try:
                ledger_file.close()
            finally:
                dataset_lock.release()
        else:
            dataset_lock.release()
    summary = summarize(manifest_id, rows)
    report = {
        "eval": "realworld-live",
        "manifest": manifest_name,
        "manifest_version": "realworld-v1",
        "baseline_prompt": PROMPT_VERSION,
        "reference_prompt": REFERENCE_PROMPT_VERSION,
        "sam_included": include_sam,
        "summary": summary,
        "rows": rows,
        "raw": raw,
        "spend_preflight_usd": preflight,
        "cost_usd": str(budget.spent),
        "spend_ledger": budget.totals(),
        "spend_ledger_path": ledger_path.name,
        "dataset_id": dataset_id,
        "markdown": render_markdown(manifest_id, summary, rows),
    }
    if pattern_mode:
        report["pattern_prompt"] = PATTERN_PROMPT_VERSION
        report["pattern_mode"] = _pattern_block(rows)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run PR #2/#3 evals")
    parser.add_argument(
        "eval",
        choices=[
            "reconcile",
            "muse-live",
            "realworld-dryrun",
            "realworld-live",
            "realworld-validate",
            "realworld-freeze",
        ],
    )
    parser.add_argument("--max-spend", type=float, default=0.25)
    parser.add_argument("--manifest", type=str, default=None)
    parser.add_argument("--photos-root", type=str, default=None)
    parser.add_argument("--recorded", type=str, default=None)
    parser.add_argument("--skip-sam", action="store_true")
    parser.add_argument(
        "--pattern-mode",
        action="store_true",
        help="Opt-in third evaluation mode using prompt m4 (cross-brand "
        "pattern guidance). Default runs keep exactly baseline (m2) and "
        "reference (m3).",
    )
    parser.add_argument(
        "--ledger",
        type=str,
        default=None,
        help="Resume audit trail: reuse an existing ledger file, which must "
        "hold no outstanding reservations (refuses otherwise).",
    )
    parser.add_argument(
        "--other-manifest",
        type=str,
        default=None,
        help="realworld-validate/freeze: second manifest for cross-split checks.",
    )
    parser.add_argument(
        "--other-photos-root",
        type=str,
        default=None,
        help="realworld-validate/freeze: photos root for the other manifest "
        "(defaults to its own directory).",
    )
    parser.add_argument(
        "--dispositions",
        type=str,
        default=None,
        help="realworld-validate/freeze: JSON file mapping 'idA|idB' to a "
        "human near-duplicate disposition.",
    )
    parser.add_argument(
        "--reviewer-role",
        type=str,
        default=None,
        help="realworld-freeze: reviewer role recording the freeze.",
    )
    parser.add_argument(
        "--replace-reason",
        type=str,
        default=None,
        help="realworld-freeze: recorded reason for replacing a freeze; "
        "archives the prior record instead of overwriting.",
    )
    parser.add_argument(
        "--register",
        type=str,
        default=None,
        help="realworld-validate: private register.json to link for admission.",
    )
    parser.add_argument(
        "--enforce-freeze",
        action="store_true",
        help="realworld-validate: fail when a holdout freeze record is missing.",
    )
    parser.add_argument(
        "--public-report",
        action="store_true",
        help="realworld-validate: print only the counts-only public aggregate.",
    )
    args = parser.parse_args()
    if args.eval == "reconcile":
        report = run_reconcile()
        name = f"{date.today().isoformat()}-reconcile-{GOLDENS_VERSION}.json"
    elif args.eval == "muse-live":
        report = run_muse_live(args.max_spend)
        name = f"{date.today().isoformat()}-muse-{PROMPT_VERSION}.json"
    elif args.eval == "realworld-validate":
        if not args.manifest:
            raise SystemExit("realworld-validate needs --manifest <path>")
        manifest_path = Path(args.manifest)
        report = run_intake_validate(
            manifest_path,
            _resolve_photos_root(manifest_path, args.photos_root),
            other_manifest=args.other_manifest,
            other_photos_root=args.other_photos_root,
            register_path=args.register,
            enforce_freeze=args.enforce_freeze,
            dispositions_path=args.dispositions,
        )
        print(format_validate_output(report, public_only=args.public_report))
        name = f"{date.today().isoformat()}-realworld-validate.json"
    elif args.eval == "realworld-freeze":
        if not args.manifest:
            raise SystemExit("realworld-freeze needs --manifest <path>")
        if not args.other_manifest:
            raise SystemExit("realworld-freeze needs --other-manifest <path>")
        if not args.reviewer_role:
            raise SystemExit("realworld-freeze needs --reviewer-role <role>")
        manifest_path = Path(args.manifest)
        report = run_intake_freeze(
            manifest_path,
            _resolve_photos_root(manifest_path, args.photos_root),
            dev_manifest_path=args.other_manifest,
            other_photos_root=args.other_photos_root,
            reviewer_role=args.reviewer_role,
            dispositions_path=args.dispositions,
            replace_reason=args.replace_reason,
        )
        print(json.dumps(report, indent=2))
        name = f"{date.today().isoformat()}-realworld-freeze.json"
    elif args.eval == "realworld-dryrun":
        if not args.manifest:
            raise SystemExit("realworld-dryrun needs --manifest <path>")
        manifest_path = Path(args.manifest)
        report = run_realworld_dryrun(
            manifest_path,
            _resolve_photos_root(manifest_path, args.photos_root),
            recorded_path=args.recorded,
            pattern_mode=args.pattern_mode,
        )
        print(report["markdown"])
        name = f"{date.today().isoformat()}-realworld-dryrun.json"
    else:
        if not args.manifest:
            raise SystemExit("realworld-live needs --manifest <path>")
        manifest_path = Path(args.manifest)
        report = run_realworld_live(
            manifest_path,
            _resolve_photos_root(manifest_path, args.photos_root),
            args.max_spend,
            include_sam=not args.skip_sam,
            ledger_path=args.ledger,
            pattern_mode=args.pattern_mode,
        )
        print(report["markdown"])
        name = f"{date.today().isoformat()}-realworld-live.json"
    dest = RESULTS / name
    dest.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    if report.get("eval") == "realworld-intake-validate":
        print(f"intake valid={report['passed']} cases={report['cases']} -> {dest}")
    elif "summary" in report:
        print(
            f"paired_cases={report['summary']['paired_cases']} "
            f"n_rows={report['summary']['n_rows']} -> {dest}"
        )
    else:
        print(
            f"{report.get('passed', '-')}/{report.get('total', '-')} passed -> {dest}"
        )
    if "passed" in report and "total" in report and report["passed"] != report["total"]:
        raise SystemExit("eval failures recorded")


if __name__ == "__main__":
    main()
