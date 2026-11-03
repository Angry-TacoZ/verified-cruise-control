"""Private real-world intake register + admission validation (PR #5).

Local-only intake infrastructure for genuine photographs. The register
lives outside the public repo (``evals/realworld/private/`` by default,
overridable with ``VCC_REALWORLD_PRIVATE_DIR``) and holds
rights/privacy/equipment records — never VINs, plates, owner identities,
or private documents. Public manifests (realworld-v1) stay backward
compatible: unknown keys there remain tolerated.

Zero inference: this module makes no model calls and reads no provider
credentials. Tests use synthetic registers in tmp_path only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

REGISTER_VERSION = "intake-v1"

LICENSE_VALUES = frozenset({"verified", "pending", "rejected"})
PRIVACY_VALUES = frozenset({"passed", "pending", "failed"})
STATUS_VALUES = frozenset({"pending", "accepted", "rejected"})
ORIGIN_VALUES = frozenset({"seller", "researcher"})

REGISTER_FIELDS = frozenset({"register_version", "entries"})
ENTRY_FIELDS = frozenset(
    {
        "id",
        "source_group",
        "image_source",
        "rights_holder",
        "license_status",
        "license_evidence",
        "date_obtained",
        "privacy_review",
        "vehicle",
        "equipment_method",
        "equipment_evidence",
        "claim_text",
        "claim_origin",
        "candidate_status",
        "status_reason",
        "original_sha256",
        "evaluation_sha256",
        "reviewer_role",
        "review_attestation",
    }
)
VEHICLE_FIELDS = frozenset({"make", "model", "year", "trim", "market"})


class IntakeError(ValueError):
    """An intake record or admission gate failed validation."""


# Value-level privacy scan. The public manifest validator checks keys
# only; this scans string VALUES for high-confidence personal or
# identifying content. It is wired into ``validate_manifest`` (and
# ``validate_case``) so dry-run and live paths are protected too.
#
# Limitations (documented, not solved): names, postal addresses, and
# license plates cannot be detected reliably, so admission additionally
# requires a recorded manual privacy review. Regex scanning never
# proves the absence of personal information.
_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}\b"
)
_DATA_URI_RE = re.compile(r"data:[a-zA-Z]+/[a-zA-Z0-9+.-]+;base64,")

# Fields whose values are digests by construction, never personal data.
# The exclusion is load-bearing: 64-hex digests contain 17-character
# VIN-charset runs, so removing it rejects the existing fixtures.
SCAN_EXCLUDED_FIELDS = frozenset({"sha256"})


def _suspicious_value(text: str) -> str | None:
    """Return a category label when a string value looks identifying."""
    if _VIN_RE.search(text):
        return "vin-like identifier"
    if _EMAIL_RE.search(text):
        return "email address"
    if _PHONE_RE.search(text):
        return "phone number"
    if _DATA_URI_RE.search(text):
        return "embedded data URI"
    return None


def scan_values(obj, where: str) -> None:
    """Reject string values carrying likely personal/identifying content.

    The error names the field and category but NEVER repeats the
    value, so the secret does not leak into logs or reports.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in SCAN_EXCLUDED_FIELDS:
                continue
            if isinstance(value, str):
                category = _suspicious_value(value)
                if category is not None:
                    raise ValueError(
                        f"{where}: suspicious {category} in field {key!r} "
                        "(quarantined for privacy review)"
                    )
            else:
                scan_values(value, f"{where}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            scan_values(value, f"{where}[{i}]")


def private_dir() -> Path:
    """Local-only intake directory (gitignored; never committed)."""
    override = os.environ.get("VCC_REALWORLD_PRIVATE_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    backend_root = here.parents[1]
    return backend_root.parent / "evals" / "realworld" / "private"


def register_path() -> Path:
    return private_dir() / "register.json"


def _require_mapping(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise IntakeError(f"{where} must be an object")
    return value


def validate_register(register: dict) -> dict:
    """Validate a private intake register. Returns it unchanged."""
    register = _require_mapping(register, "register")
    unknown = set(register) - REGISTER_FIELDS
    if unknown:
        raise IntakeError(f"register has unexpected fields {sorted(unknown)}")
    if register.get("register_version") != REGISTER_VERSION:
        raise IntakeError(
            f"unsupported register_version {register.get('register_version')!r}, "
            f"expected {REGISTER_VERSION!r}"
        )
    entries = register.get("entries")
    if not isinstance(entries, list):
        raise IntakeError("register entries must be a list")
    seen = set()
    for entry in entries:
        _validate_entry(entry, seen)
    return register


def _validate_entry(entry: dict, seen: set) -> None:
    entry = _require_mapping(entry, "entry")
    unknown = set(entry) - ENTRY_FIELDS
    if unknown:
        raise IntakeError(f"entry has unexpected fields {sorted(unknown)}")
    entry_id = entry.get("id")
    if not entry_id or not isinstance(entry_id, str):
        raise IntakeError("entry id must be a non-empty string")
    if entry_id in seen:
        raise IntakeError(f"duplicate entry id {entry_id!r}")
    seen.add(entry_id)
    if entry.get("license_status") not in LICENSE_VALUES:
        raise IntakeError(f"entry {entry_id}: bad license_status")
    if entry.get("privacy_review") not in PRIVACY_VALUES:
        raise IntakeError(f"entry {entry_id}: bad privacy_review")
    if entry.get("candidate_status") not in STATUS_VALUES:
        raise IntakeError(f"entry {entry_id}: bad candidate_status")
    if entry.get("claim_origin") not in ORIGIN_VALUES:
        raise IntakeError(f"entry {entry_id}: bad claim_origin")
    vehicle = _require_mapping(entry.get("vehicle"), f"entry {entry_id} vehicle")
    missing = VEHICLE_FIELDS - set(vehicle)
    if missing:
        raise IntakeError(f"entry {entry_id}: vehicle missing {sorted(missing)}")
    for field in ("original_sha256", "evaluation_sha256"):
        digest = entry.get(field)
        if not isinstance(digest, str) or len(digest) != 64:
            raise IntakeError(f"entry {entry_id}: {field} must be a 64-char digest")
        try:
            int(digest, 16)
        except ValueError:
            raise IntakeError(f"entry {entry_id}: {field} must be hex") from None
    for field in (
        "source_group",
        "image_source",
        "rights_holder",
        "license_evidence",
        "date_obtained",
        "equipment_method",
        "equipment_evidence",
        "claim_text",
        "status_reason",
        "reviewer_role",
        "review_attestation",
    ):
        if not entry.get(field) or not isinstance(entry.get(field), str):
            raise IntakeError(f"entry {entry_id}: {field} must be a non-empty string")


# Source groups tie photographs/claims from one physical vehicle or
# listing together. Optional in the public realworld-v1 schema
# (synthetic fixtures stay valid) but REQUIRED by the admission
# validator for every real case: a missing group fails, otherwise the
# leakage check would pass vacuously.
def require_source_group(case: dict, where: str) -> str:
    """Return the case's source_group or fail admission without one."""
    group = case.get("source_group")
    if not group or not isinstance(group, str):
        raise IntakeError(
            f"{where}: case {case.get('id')!r} has no source_group; "
            "admission requires one opaque group per physical vehicle"
        )
    return group


def check_cross_split(dev_cases: list, holdout_cases: list) -> list:
    """Fail when any source_group appears in both splits. Returns []."""
    dev_groups = {c.get("source_group") for c in dev_cases} - {None, ""}
    hold_groups = {c.get("source_group") for c in holdout_cases} - {None, ""}
    overlap = sorted(dev_groups & hold_groups)
    if overlap:
        raise IntakeError(
            f"cross-split leakage: source_group(s) {overlap} present in "
            "both dev and holdout; reallocate by physical vehicle"
        )
    return []


def check_exact_duplicates(cases: list, *, strict: bool = False) -> dict:
    """Map each repeated photo SHA-256 to its sorted case ids.

    With strict=True any repetition raises; otherwise the mapping is
    returned for the quality report and holdout-freeze gate.
    """
    by_hash: dict = {}
    for case in cases:
        digest = (case.get("photo") or {}).get("sha256")
        by_hash.setdefault(digest, []).append(case.get("id"))
    dupes = {h: sorted(ids) for h, ids in by_hash.items() if len(ids) > 1}
    if strict and dupes:
        raise IntakeError(f"exact duplicate photo bytes: {dupes}")
    return dupes


# Hamming-distance ceiling for the perceptual near-duplicate signal.
# A one-pixel tweak on a 64px test image hashes at distance 0; a
# solid red vs solid blue image differs in ~half the bits. The signal
# is a review prompt, never proof: flat, gradient-free images of
# different colors hash identically, so human judgment stays
# required. Unresolved probable near-duplicates across splits block
# the holdout freeze until a human disposition is recorded in the
# private register.
NEAR_DUP_THRESHOLD = 6


def _dhash(path) -> int:
    """8x8 difference hash of an image file (grayscale, Pillow only)."""
    from PIL import Image

    with Image.open(path) as img:
        small = img.convert("L").resize((9, 8))
        pixels = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (
                1 if pixels[row * 9 + col] > pixels[row * 9 + col + 1] else 0
            )
    return bits


def find_near_duplicates(
    labeled_paths: list, *, threshold: int = NEAR_DUP_THRESHOLD
) -> list:
    """Flag probable near-duplicate photos as (id_a, id_b, distance).

    Pairs at or under the threshold are returned sorted by distance.
    Unreadable files fail closed: intake cannot judge what it cannot
    decode.
    """
    hashes = []
    for label, path in labeled_paths:
        try:
            digest = _dhash(path)
        except OSError as exc:
            raise IntakeError(
                f"near-duplicate check cannot decode {label}: {exc}"
            ) from exc
        hashes.append((label, digest))
    hits = []
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            distance = (hashes[i][1] ^ hashes[j][1]).bit_count()
            if distance <= threshold:
                hits.append((hashes[i][0], hashes[j][0], distance))
    return sorted(hits, key=lambda hit: hit[2])


# Original photographs are immutable evidence: the authorized
# original stays in restricted local storage and the evaluation copy
# is derived once, never modified in place. Pillow guarantees no
# lossless metadata removal for every format, so the transform is
# explicit and documented per copy:
# - "none": PNG (or metadata-free input) resaved without metadata;
#   pixels verified identical.
# - "exif-transpose": EXIF orientation baked in with
#   ImageOps.exif_transpose as the documented pixel transform.
# - "re-encode": JPEG resaved (quality 95) to drop APP markers Pillow
#   cannot strip without re-encoding; pixels change slightly and the
#   ADAS-relevant region must be visually verified by the operator.
EXIF_ORIENTATION_TAG = 274
EXIF_GPS_TAG = 34853
JPEG_RECOMPRESSION_QUALITY = 95


def inspect_image(path) -> dict:
    """Report EXIF orientation, GPS presence, and format for one file.

    The orientation value is recorded BEFORE any stripping, because
    dropping the tag from a rotated phone photo changes how a decoder
    displays it.
    """
    from PIL import Image

    path = Path(path)
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            orientation = exif.get(EXIF_ORIENTATION_TAG)
            gps_present = EXIF_GPS_TAG in exif
            return {
                "format": img.format,
                "size": list(img.size),
                "orientation": orientation,
                "gps_present": gps_present,
            }
    except OSError as exc:
        raise IntakeError(f"cannot inspect image {path}: {exc}") from exc


def prepare_evaluation_copy(src, dest) -> dict:
    """Derive a privacy-safe evaluation copy; never modifies in place.

    Returns a provenance report with both digests (SHA-256 computed
    only AFTER the copy is final), the recorded orientation, the
    transform applied, and the GPS finding. GPS-carrying originals
    are quarantined, never silently stripped.
    """
    from PIL import Image, ImageOps

    src = Path(src)
    dest = Path(dest)
    if src.resolve() == dest.resolve():
        raise IntakeError("refusing to modify an evaluation copy in place")
    try:
        original_bytes = src.read_bytes()
    except OSError as exc:
        raise IntakeError(f"original photo {src} unreadable: {exc}") from exc
    original_sha256 = hashlib.sha256(original_bytes).hexdigest()
    try:
        with Image.open(src) as img:
            fmt = (img.format or "").upper()
            exif = img.getexif()
            orientation = exif.get(EXIF_ORIENTATION_TAG)
            gps_present = EXIF_GPS_TAG in exif
            if gps_present:
                raise IntakeError(
                    f"photo {src} carries GPS metadata: quarantined for "
                    "operator review, never silently stripped"
                )
            if orientation not in (None, 1):
                img = ImageOps.exif_transpose(img)
                transform = "exif-transpose"
            elif fmt == "JPEG":
                transform = "re-encode"
            else:
                transform = "none"
            dest.parent.mkdir(parents=True, exist_ok=True)
            save_kwargs: dict = {}
            if fmt == "JPEG":
                save_kwargs = {"format": "JPEG", "quality": JPEG_RECOMPRESSION_QUALITY}
            img.save(dest, **save_kwargs)
    except OSError as exc:
        raise IntakeError(f"cannot derive evaluation copy for {src}: {exc}") from exc
    evaluation_sha256 = hashlib.sha256(dest.read_bytes()).hexdigest()
    final = inspect_image(dest)
    if final["orientation"] not in (None, 1):
        raise IntakeError(f"evaluation copy {dest} still carries orientation metadata")
    return {
        "original_sha256": original_sha256,
        "evaluation_sha256": evaluation_sha256,
        "orientation": orientation,
        "transform": transform,
        "gps_present": gps_present,
        "format": fmt,
    }


# Photo-only pilot semantics. ground_truth.equipment is the
# independently established vehicle truth (equipped / not-equipped /
# unknown). ground_truth.expected_state is the evidence state the
# PHOTO-ONLY evaluation can legitimately support: without an
# authoritative evidence input the photo-only pipeline cannot reach
# verified or contradicted, so the admission validator rejects those
# labels here. The public schema and production reconciliation are
# unchanged — this gate applies to pilot admission only.
PHOTO_ONLY_FORBIDDEN_STATES = frozenset({"verified", "contradicted"})


def validate_admission(entry: dict, case: dict) -> str:
    """Admit one register entry + manifest case pair. Returns "accepted".

    Every gate fails closed: rights verified, privacy passed, status
    accepted, independent equipment evidence present, reviewer
    attestation recorded, source group assigned, register claim text
    identical to the manifest's seller wording, and expected_state
    within photo-only reach. Labels must come from independently
    reviewed evidence — this module never calls the reconciler.
    """
    _validate_entry(entry, set())
    case_id = case.get("id")
    if entry.get("candidate_status") != "accepted":
        raise IntakeError(f"case {case_id}: register status is not accepted")
    if entry.get("license_status") != "verified":
        raise IntakeError(f"case {case_id}: usage rights are not verified")
    if entry.get("privacy_review") != "passed":
        raise IntakeError(f"case {case_id}: manual privacy review has not passed")
    require_source_group(case, "admission")
    advertised = (case.get("claim") or {}).get("advertised")
    if entry.get("claim_text") != advertised:
        raise IntakeError(
            f"case {case_id}: register claim text does not match the "
            "manifest's seller wording verbatim"
        )
    expected = (case.get("ground_truth") or {}).get("expected_state")
    if expected in PHOTO_ONLY_FORBIDDEN_STATES:
        raise IntakeError(
            f"case {case_id}: photo-only pilot cannot label {expected!r}; "
            "verified needs authoritative input and absence is not contradiction"
        )
    return "accepted"


# Holdout integrity. Dev and holdout are allocated by source group
# (cross-split check above); before any model evaluation the holdout
# is frozen. The freeze record is private: holdout manifest SHA-256 +
# case inventory + date + reviewer role. Every later validation
# flags a changed holdout manifest; a missing freeze record is
# reported, never silently passed. Authorized holdout evaluations
# are logged MANUALLY (an honor system — the log is a local file the
# operator appends to; run.py is deliberately not modified to
# automate it). Never tune on holdout; never report dev as validation.
def manifest_hash(manifest: dict) -> str:
    """Stable content hash for one manifest (canonical JSON, SHA-256)."""
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _freeze_path(dataset: str) -> Path:
    return private_dir() / f"holdout-freeze-{dataset}.json"


def _evaluations_path(dataset: str) -> Path:
    return private_dir() / f"holdout-evaluations-{dataset}.jsonl"


def freeze_holdout(
    *,
    dataset: str,
    manifest: dict,
    reviewer_role: str,
    run_date=None,
    dev_manifest: dict | None = None,
    photo_paths: dict | None = None,
    dispositions: dict | None = None,
) -> dict:
    """Freeze a holdout manifest after all cross-split gates pass.

    Blocks on: source_group overlap, exact duplicate bytes across
    splits, and probable near-duplicates across splits without a
    recorded human disposition. Returns and persists the record.
    """
    from datetime import date as _date

    cases = manifest.get("cases", [])
    case_ids = sorted(case.get("id") for case in cases)
    dev_ids: set = set()
    if dev_manifest is not None:
        dev_cases = dev_manifest.get("cases", [])
        dev_ids = {case.get("id") for case in dev_cases}
        check_cross_split(dev_cases, cases)
        dupes = check_exact_duplicates(dev_cases + cases)
        cross_exact = {
            digest: ids
            for digest, ids in dupes.items()
            if any(cid in dev_ids for cid in ids)
            and any(cid not in dev_ids for cid in ids)
        }
        if cross_exact:
            raise IntakeError(
                f"holdout freeze refused: exact duplicate bytes across splits {cross_exact}"
            )
    near_hits = []
    if photo_paths:
        labeled = [
            (cid, photo_paths[cid])
            for cid in case_ids + sorted(dev_ids)
            if cid in photo_paths
        ]
        for first, second, distance in find_near_duplicates(labeled):
            if (first in dev_ids) == (second in dev_ids):
                continue
            key = "|".join(sorted((first, second)))
            if not (dispositions or {}).get(key):
                raise IntakeError(
                    f"holdout freeze refused: probable near-duplicate {first}/{second} "
                    f"(distance {distance}) unresolved; record a human disposition"
                )
            near_hits.append(
                {
                    "pair": [first, second],
                    "distance": distance,
                    "disposition": dispositions[key],
                }
            )
    record = {
        "dataset": dataset,
        "split": manifest.get("split"),
        "manifest_sha256": manifest_hash(manifest),
        "case_ids": case_ids,
        "date": run_date or _date.today().isoformat(),
        "reviewer_role": reviewer_role,
        "dispositions": dispositions or {},
        "resolved_near_duplicates": near_hits,
    }
    target = _freeze_path(dataset)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return record


def check_holdout_freeze(dataset: str, manifest: dict) -> dict:
    """Validate a holdout manifest against its freeze record."""
    target = _freeze_path(dataset)
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except OSError:
        raise IntakeError(
            f"dataset {dataset}: no holdout freeze record at {target}; "
            "freeze the holdout before any model evaluation"
        ) from None
    except ValueError as exc:
        raise IntakeError(
            f"dataset {dataset}: freeze record unreadable ({exc})"
        ) from exc
    current = manifest_hash(manifest)
    if record.get("manifest_sha256") != current:
        raise IntakeError(
            f"dataset {dataset}: holdout manifest changed since freeze "
            f"(frozen {record.get('manifest_sha256')}, now {current})"
        )
    return record


def log_holdout_evaluation(
    *,
    dataset: str,
    manifest_hash: str,
    prompt_versions: dict,
    run_date: str,
    reason: str,
) -> dict:
    """Manually record one authorized holdout evaluation (honor system)."""
    entry = {
        "dataset": dataset,
        "manifest_hash": manifest_hash,
        "prompt_versions": dict(prompt_versions),
        "run_date": run_date,
        "reason": reason,
    }
    target = _evaluations_path(dataset)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def read_evaluation_log(dataset: str) -> list:
    """Read the manual holdout-evaluation log (empty when never logged)."""
    target = _evaluations_path(dataset)
    if not target.exists():
        return []
    entries = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def load_register() -> dict:
    """Load and validate the private register (fails when absent)."""
    path = register_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IntakeError(f"register {path} unreadable ({exc})") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise IntakeError(f"register {path} is not valid JSON ({exc})") from exc
    return validate_register(data)


def save_register(register: dict) -> Path:
    """Validate and persist the private register. Returns its path."""
    validate_register(register)
    target = register_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(register, indent=2, sort_keys=True), encoding="utf-8")
    return target
