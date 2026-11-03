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
            distance = bin(hashes[i][1] ^ hashes[j][1]).count("1")
            if distance <= threshold:
                hits.append((hashes[i][0], hashes[j][0], distance))
    return sorted(hits, key=lambda hit: hit[2])


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
