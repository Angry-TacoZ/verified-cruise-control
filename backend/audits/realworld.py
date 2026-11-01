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
import os
import uuid
from decimal import Decimal
from pathlib import Path

from audits.models import Observation
from audits.pipeline import control_extra, sam_target_for

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

# Optional independent localization ground truth: a human-verified label
# stating whether SAM should localize the observed object in this photo.
# Absent by default — localization is then reported as not measured, never
# inferred from text agreement.
LOCALIZATION_OUTCOMES = ("localized", "no-mask")


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
    localization = truth.get("localization")
    if localization is not None:
        if not isinstance(localization, dict):
            raise ValueError(f"case {case['id']}: localization must be an object")
        if localization.get("verified_outcome") not in LOCALIZATION_OUTCOMES:
            raise ValueError(
                f"case {case['id']}: bad localization.verified_outcome "
                f"{localization.get('verified_outcome')!r}"
            )
        if not localization.get("verified_by") or not isinstance(
            localization.get("verified_by"), str
        ):
            raise ValueError(
                f"case {case['id']}: localization.verified_by must name the "
                "independent verification method"
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


# Tokens too generic to identify a control on their own: sharing only
# these must never count as a match.
_CONTROL_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "and",
        "or",
        "for",
        "to",
        "in",
        "on",
        "with",
        "button",
        "buttons",
        "control",
        "controls",
        "switch",
        "icon",
    }
)


def _control_tokens(text: str) -> frozenset:
    import re

    return frozenset(
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in _CONTROL_STOPWORDS
    )


def control_matches(predicted: str, expected: str) -> bool:
    """Decide whether a predicted control names the expected object.

    Exact match (after case/whitespace normalization) wins; otherwise
    the substantive token sets must be equal, or the smaller must be
    contained in the larger with at least two shared tokens. Empty on
    either side never matches, and sharing only generic words
    ("button", "control", "icon", ...) never matches either.
    """
    predicted_norm = " ".join((predicted or "").split()).lower()
    expected_norm = " ".join((expected or "").split()).lower()
    if not predicted_norm or not expected_norm:
        return False
    if predicted_norm == expected_norm:
        return True
    predicted_tokens = _control_tokens(predicted_norm)
    expected_tokens = _control_tokens(expected_norm)
    if not predicted_tokens or not expected_tokens:
        return False
    if predicted_tokens == expected_tokens:
        return True
    shared = predicted_tokens & expected_tokens
    return len(shared) >= 2 and (
        predicted_tokens <= expected_tokens or expected_tokens <= predicted_tokens
    )


def muse_verdict(item: dict) -> str:
    """Map one validated Muse observation to present/absent/unclear.

    Production-identical rule (mirrors ``pipeline._verdict``): ambiguous
    is unclear, literal "none" is absent, anything else is present.
    """
    if item.get("ambiguous"):
        return "unclear"
    if str(item.get("visible_object", "") or "").strip().lower() == "none":
        return "absent"
    return "present"


def build_eval_observation(item: dict, *, claim_id: str, photo_id: str) -> Observation:
    """Build an unsaved Observation carrying the complete validated data.

    Extra uses the production projection (localization flags plus full
    Layer B control identification: symbols, locations, dedicated flags,
    manufacturer context, and validated nested ``controls``), so the
    existing interpretation and reconciliation machinery sees exactly
    what production would persist — nothing dropped, nothing invented.
    The row carries its own id so SAM provenance can link back to it,
    exactly like production's ``source_observation_id``.
    """
    return Observation(
        id=uuid.uuid4(),
        source="muse",
        verdict=muse_verdict(item),
        photo_id=uuid.uuid4(),
        rationale=str(item.get("description", "") or ""),
        extra={
            "claim_id": claim_id,
            "eval_photo_id": str(photo_id),
            "supports_localization": bool(item.get("supports_localization")),
            "needs_crop": bool(item.get("needs_crop")),
            "needs_other_view": bool(item.get("needs_other_view")),
            **control_extra(item),
        },
    )


def select_sam_target(eval_obs, claim_text: str, vehicle=None):
    """Production SAM target selection for one evaluation observation.

    Reuses ``pipeline.sam_target_for`` (top-level plus validated nested
    controls, claim-relevant first) and honors production's eligibility
    rules: only PRESENT observations flagged for localization are
    localizable. Returns ``(concept, target_source, control_index)``, or
    ``None`` when production would make no call — in which case the
    evaluator must not call SAM either.
    """
    if eval_obs.verdict != "present":
        return None
    if not (eval_obs.extra or {}).get("supports_localization"):
        return None
    concept, target_source, control_index = sam_target_for(
        eval_obs, claim_text, vehicle
    )
    if not concept:
        return None
    return (concept, target_source, control_index)


def build_sam_eval_observation(
    muse_eval_obs, *, concept: str, target_source: str, control_index, masks: int
) -> Observation:
    """Build an unsaved SAM observation with production-identical shape.

    Verdict is PRESENT only when masks were returned (else ABSENT, exactly
    like production); provenance links the same photo plus the source
    observation id and control index, so cross-photo or cross-control
    localizations can never corroborate the wrong evidence.
    """
    return Observation(
        id=uuid.uuid4(),
        source="sam",
        verdict="present" if masks > 0 else "absent",
        photo_id=muse_eval_obs.photo_id,
        rationale=(
            f"{masks} segment(s) for concept {concept!r}"
            if masks
            else f"no segments for concept {concept!r}"
        ),
        extra={
            "sam_target": concept,
            "target_source": target_source,
            "control_index": control_index,
            "source_observation_id": str(muse_eval_obs.id),
        },
    )


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

    control_correct = control_matches(control, visible)
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
    # Target SELECTION correctness: the SAM prompt named the independently
    # established object (text agreement only — never localization proof).
    sam_target_selected = control_matches(sam_target, visible)
    # LOCALIZATION correctness is scored only against an independent
    # verified label; otherwise it stays None (not measured).
    sam_masks = recorded.get("sam_masks", 0) or 0
    localization_gt = (truth.get("localization") or {}).get("verified_outcome")
    if localization_gt is None:
        sam_localization_correct = None
    else:
        sam_localization_correct = (sam_masks > 0) == (localization_gt == "localized")

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
        "sam_target_selected": sam_target_selected,
        "sam_masks_observed": sam_masks,
        "sam_localization_correct": sam_localization_correct,
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
        "sam_target_selected": sum(1 for r in scored if r.get("sam_target_selected")),
        "sam_localization_measured": sum(
            1 for r in scored if r.get("sam_localization_correct") is not None
        ),
        "sam_localization_accuracy": _rate(
            sum(1 for r in scored if r.get("sam_localization_correct") is True),
            sum(1 for r in scored if r.get("sam_localization_correct") is not None),
        ),
        "missing_or_unreadable": sum(
            1
            for r in rows
            if r.get("status") in ("missing-photo", "unreadable", "missing-observation")
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


class BudgetRefused(Exception):
    """A provider call was refused before sending: unauthorized, over the
    call-count allowance, over the spend bound, frozen by unknown cost,
    or blocked by an outstanding reservation from an earlier run."""


class LedgerFile:
    """Append-only durable ledger file.

    Every event is serialized, flushed, and fsync'd before the call
    returns, so a hard crash loses nothing already reported. The file is
    opened for appending only: events are never rewritten or deleted,
    and one run never overwrites another run's file.
    """

    def __init__(self, path) -> None:
        self.path = Path(path)
        # The handle lifetime is the object lifetime; closed explicitly
        # via close() (LedgerFile is also usable as a context manager).
        self._handle = open(self.path, "ab")  # noqa: SIM115

    def append(self, record: dict) -> None:
        import json

        line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        self._handle.write(line)
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
        finally:
            self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def read_ledger(path) -> list:
    """Read persisted ledger events; a missing file reads as empty."""
    import json

    target = Path(path)
    if not target.exists():
        return []
    records = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def find_outstanding(records: list, dataset_id=None) -> list:
    """Reserved events with no matching settlement, in ledger order.

    Settlements with unknown cost count as outstanding: the billing status
    is unresolved, so a restart must not assume zero spend. When
    ``dataset_id`` is given, only records for that dataset — plus legacy
    records carrying no dataset identity, treated conservatively — block.
    A reserved record without a token can never match a settlement.
    """
    settled = {
        record.get("token")
        for record in records
        if record.get("event") in ("settled", "settled-unknown")
    }
    blocking = []
    for record in records:
        event = record.get("event")
        unresolved = event == "settled-unknown" or (
            event == "reserved" and record.get("token") not in settled
        )
        same_dataset = dataset_id is None or record.get("dataset_id") in (
            None,
            dataset_id,
        )
        if unresolved and same_dataset:
            blocking.append(record)
    return blocking


def _describe_blockers(outstanding: list) -> str:
    labels = sorted({str(item.get("label", "?")) for item in outstanding})
    runs = sorted({str(item.get("run_id", "?")) for item in outstanding})
    return f"{len(outstanding)} outstanding reservation(s) ({', '.join(labels)}; runs: {', '.join(runs)})"


def assert_ledger_clear(path, dataset_id=None) -> None:
    """Fail closed when an earlier run left outstanding reservations.

    Never auto-replays: the operator reconciles billing externally, then
    archives or removes the file before rerunning.
    """
    outstanding = find_outstanding(read_ledger(path), dataset_id)
    if outstanding:
        raise BudgetRefused(
            f"ledger {path} has {_describe_blockers(outstanding)}: reconcile "
            "provider billing, then archive or remove the file before "
            "rerunning. Calls are never auto-replayed."
        )


def fresh_ledger_path(directory, stem: str, run_id: str):
    """Unique ledger path per run so runs never overwrite one another."""
    from datetime import date

    return Path(directory) / f"{date.today().isoformat()}-{stem}-{run_id}.jsonl"


def dataset_fingerprint(manifest) -> str:
    """Stable content-based identity for one evaluation dataset.

    Derived from the manifest content (version, dataset, split, and per
    case the id, photo hash, and claim) so renaming files cannot evade
    cross-run protection, while a demonstrably different dataset hashes
    differently.
    """
    import json as _json

    if isinstance(manifest, (str, Path)):
        manifest = _json.loads(Path(manifest).read_text(encoding="utf-8"))
    cases = []
    for case in manifest.get("cases", []):
        claim = case.get("claim", {})
        cases.append(
            {
                "id": case.get("id"),
                "photo_sha256": (case.get("photo") or {}).get("sha256"),
                "advertised": claim.get("advertised"),
                "normalized_feature": claim.get("normalized_feature"),
            }
        )
    canonical = _json.dumps(
        {
            "manifest_version": manifest.get("manifest_version"),
            "dataset": manifest.get("dataset"),
            "split": manifest.get("split"),
            "cases": sorted(cases, key=lambda item: str(item.get("id"))),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


LEDGER_GLOB = "*-realworld-live-*.jsonl"
LOCK_TTL_SECONDS = 600


def ledger_lock_path(directory, dataset_id: str):
    """Coordination lock for one dataset (no new infrastructure)."""
    return Path(directory) / f".realworld-live.{dataset_id[:16]}.lock"


def acquire_ledger_lock(
    path, run_id: str, *, ttl_seconds: int = LOCK_TTL_SECONDS
) -> dict:
    """Claim the dataset coordination lock, or refuse.

    Creation is atomic, so two simultaneous contenders cannot both win.
    A lock older than ``ttl_seconds`` is treated as a crashed holder and
    taken over; anything else held refuses. A malformed lock file fails
    closed. Returns the lock payload.
    """
    import json as _json
    import time as _time

    payload = {"run_id": run_id, "pid": os.getpid(), "created": _time.time()}
    line = _json.dumps(payload, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            existing = _json.loads(Path(path).read_text(encoding="utf-8"))
            created = float(existing.get("created", 0))
            holder = str(existing.get("run_id", "?"))
        except (OSError, ValueError, TypeError):
            raise BudgetRefused(
                f"ledger lock {path} is malformed: refusing evaluation until "
                "it is inspected and removed by the operator."
            )
        if _time.time() - created <= ttl_seconds:
            raise BudgetRefused(
                f"another evaluation (run {holder}) holds the ledger lock "
                f"{path}: refusing concurrent paid runs for this dataset."
            )
        Path(path).unlink()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "wb") as handle:
        handle.write(line + b"\n")
    return payload


def release_ledger_lock(path, run_id: str) -> None:
    """Release our own lock; a foreign holder's lock is left alone."""
    import json as _json

    try:
        existing = _json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    if existing.get("run_id") == run_id:
        Path(path).unlink(missing_ok=True)


def scan_ledger_dir(directory, dataset_id: str) -> list:
    """Find same-dataset ledgers with outstanding reservations.

    Returns ``[(path, outstanding)]`` hits. Unreadable or malformed
    relevant files fail closed instead of being silently skipped; legacy
    records without dataset identity are treated conservatively.
    """
    hits = []
    for path in sorted(Path(directory).glob(LEDGER_GLOB)):
        try:
            records = read_ledger(path)
        except (OSError, ValueError) as exc:
            raise BudgetRefused(
                f"ledger {path} is unreadable or malformed ({exc}): refusing "
                "evaluation until it is inspected, reconciled, or archived."
            )
        outstanding = find_outstanding(records, dataset_id)
        if outstanding:
            hits.append((path, outstanding))
    return hits


def refuse_if_blocked(hits: list) -> None:
    """Refuse new paid inference while prior reservations are unresolved."""
    if not hits:
        return
    parts = [
        f"{path.name}: {_describe_blockers(outstanding)}" for path, outstanding in hits
    ]
    raise BudgetRefused(
        "refusing paid evaluation: unresolved reservations from previous "
        f"run(s) ({'; '.join(parts)}). Reconcile provider billing, then "
        "archive the ledger file(s) before rerunning. Unknown billing is "
        "never zero; requests are never auto-replayed."
    )


class EvalBudget:
    """Pre-call reservation ledger for offline evaluation (no Django job).

    Mirrors the production ``reserve_call``/``settle_call`` semantics in a
    dependency-free form with write-ahead durability: every provider call
    is reserved BEFORE it is sent — call-count headroom (first try plus
    bounded retries) and spend headroom (full per-call reserve) — and the
    reservation is appended to the ledger file (flush + fsync) BEFORE the
    adapter fires, so a hard crash mid-call still leaves an identifiable
    outstanding reservation. Settlement is appended the same way after
    the call, booking actual attempts, provider, and cost. Unknown cost
    freezes further spending instead of settling as zero. Every event
    also stays in ``records`` for the in-process report.
    """

    def __init__(
        self,
        *,
        authorized: bool,
        max_spend_usd: str,
        max_attempts_total: int,
        reserve_usd_per_call: str,
        ledger_file=None,
        run_id=None,
        dataset_id=None,
    ) -> None:
        self.authorized = authorized
        self.max_spend = Decimal(max_spend_usd)
        self.max_attempts_total = max_attempts_total
        self.reserve_usd = Decimal(reserve_usd_per_call)
        self.ledger_file = ledger_file
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.dataset_id = dataset_id
        self.spent = Decimal(0)
        self.reserved_spend = Decimal(0)
        self.attempts_used = 0
        self.attempts_reserved = 0
        self.unknown_cost = False
        self.records: list = []
        self._outstanding: dict = {}
        self._seq = 0

    def _emit(self, event: str, label: str, **fields) -> None:
        self._seq += 1
        record = {
            "seq": self._seq,
            "event": event,
            "run_id": self.run_id,
            "dataset_id": self.dataset_id,
            "label": label,
            **fields,
        }
        if self.ledger_file is not None:
            # Durable first: a crash after this point leaves the event on
            # disk even if the in-memory copy is lost.
            self.ledger_file.append(record)
        self.records.append(record)

    def reserve(
        self,
        label: str,
        *,
        max_attempts: int,
        provider: str = "?",
        model: str = "?",
        case_id=None,
        mode=None,
    ) -> str:
        """Reserve headroom for one provider call; returns a token.

        The reservation is fsync'd to the ledger BEFORE the token is
        returned, so the adapter (called after) can never run ahead of
        durable accounting. Refuses (raising ``BudgetRefused``) without
        authorization, after unknown cost, or when attempts or spend
        headroom is insufficient.
        """
        if not self.authorized:
            raise BudgetRefused(f"{label}: eval inference not authorized")
        if self.unknown_cost:
            raise BudgetRefused(f"{label}: spend frozen after unknown cost")
        if max_attempts < 1:
            raise BudgetRefused(f"{label}: max_attempts must be >= 1")
        if self.attempts_used + self.attempts_reserved + max_attempts > (
            self.max_attempts_total
        ):
            raise BudgetRefused(
                f"{label}: needs {max_attempts} attempt(s), "
                f"{self.max_attempts_total - self.attempts_used - self.attempts_reserved} left"
            )
        if self.spent + self.reserved_spend + self.reserve_usd > self.max_spend:
            raise BudgetRefused(
                f"{label}: ${self.reserve_usd} headroom exceeds remaining "
                f"${self.max_spend - self.spent - self.reserved_spend}"
            )
        token = f"{self.run_id}:{self._seq + 1:06d}"
        self._emit(
            "reserved",
            label,
            token=token,
            provider=provider,
            model=model,
            case_id=case_id,
            mode=mode,
            max_attempts=max_attempts,
            reserve_usd=str(self.reserve_usd),
        )
        self._outstanding[token] = {
            "label": label,
            "max_attempts": max_attempts,
            "reserve_usd": str(self.reserve_usd),
        }
        self.attempts_reserved += max_attempts
        self.reserved_spend += self.reserve_usd
        return token

    def settle(
        self,
        token: str,
        *,
        attempts: int,
        cost_usd,
        provider: str,
    ) -> None:
        """Release a reservation and book actuals, durably. ``cost_usd=None``
        records unknown billing and freezes the ledger."""
        held = self._outstanding.pop(token, None)
        label = (held or {}).get("label", token)
        self.attempts_reserved = max(
            0, self.attempts_reserved - (held or {}).get("max_attempts", 0)
        )
        self.reserved_spend = max(
            Decimal(0), self.reserved_spend - Decimal(str(self.reserve_usd))
        )
        self.attempts_used += attempts
        if cost_usd is None:
            self.unknown_cost = True
            self._emit(
                "settled-unknown",
                label,
                token=token,
                attempts=attempts,
                provider=provider,
            )
            return
        self.spent += Decimal(str(cost_usd))
        self._emit(
            "settled",
            label,
            token=token,
            attempts=attempts,
            cost_usd=str(Decimal(str(cost_usd))),
            provider=provider,
        )

    def guarded(
        self,
        label: str,
        adapter,
        request,
        *,
        max_attempts: int,
        case_id=None,
        mode=None,
    ):
        """Reserve, run one adapter call, and settle with observed usage.

        The adapter never fires unless the reservation is already durable
        on disk. Provider exceptions settle as unknown cost (freezing the
        ledger) and are re-raised so the run aborts instead of silently
        continuing — never auto-replaying a possibly billed request.
        """
        token = self.reserve(
            label,
            max_attempts=max_attempts,
            provider=getattr(adapter, "provider_name", "?"),
            model=getattr(adapter, "model_id", "?"),
            case_id=case_id,
            mode=mode,
        )
        try:
            result = adapter.analyze(request)
        except Exception:
            attempts = getattr(adapter, "last_attempts", max_attempts)
            self.settle(
                token,
                attempts=attempts,
                cost_usd=None,
                provider=getattr(adapter, "provider_name", "?"),
            )
            raise
        usage = result.usage
        self.settle(
            token,
            attempts=1 + (usage.retries or 0),
            cost_usd=usage.estimated_cost_usd,
            provider=usage.provider,
        )
        return result

    def totals(self) -> dict:
        return {
            "run_id": self.run_id,
            "spent_usd": str(self.spent),
            "attempts_used": self.attempts_used,
            "unknown_cost": self.unknown_cost,
            "outstanding": sorted(self._outstanding),
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
            "SAM target-selection count",
            base["sam_target_selected"],
            base["n"],
            ref["sam_target_selected"],
            ref["n"],
            "count",
        ),
        (
            "SAM localization accuracy",
            base["sam_localization_accuracy"],
            base["sam_localization_measured"],
            ref["sam_localization_accuracy"],
            ref["sam_localization_measured"],
            "rate",
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
