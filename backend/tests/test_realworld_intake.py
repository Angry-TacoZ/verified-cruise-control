"""Unit tests for real-world intake/validation (PR #5).

All tests are offline and use synthetic data in tmp_path only.
No provider calls, no credentials, no real photographs.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Private intake paths that must never be committed. The register and
# every associated private evidence directory live under these.
MUST_BE_IGNORED = [
    "evals/realworld/private/",
    "evals/realworld/private/register.json",
    "evals/realworld/private/holdout-freeze.json",
    "evals/realworld/photos/some-photo.jpg",
    "evals/realworld/manifest.pilot.json",
]


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        check=False,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_private_intake_paths_are_gitignored():
    """Every private intake path must be ignored by git.

    Fails if any listed path is not ignored, so a missing .gitignore
    rule is caught before private material can be staged.
    """
    missing = []
    for rel in MUST_BE_IGNORED:
        result = _git("check-ignore", "-q", rel)
        if result.returncode != 0:
            missing.append(rel)
    assert not missing, f"not gitignored (would be committable): {missing}"


def _register_entry(**overrides):
    entry = {
        "id": "intake-001",
        "source_group": "group-a1b2c3",
        "image_source": "rights holder name on file",
        "rights_holder": "rights holder on file",
        "license_status": "verified",
        "license_evidence": "permission record 2026-11-01 on file",
        "date_obtained": "2026-11-01",
        "privacy_review": "passed",
        "vehicle": {
            "make": "Kia",
            "model": "Forte",
            "year": 2021,
            "trim": "GT-Line",
            "market": "US",
        },
        "equipment_method": "original window sticker on file",
        "equipment_evidence": "sticker record 2026-11-01 on file",
        "claim_text": "Lane Keeping Assist",
        "claim_origin": "seller",
        "candidate_status": "accepted",
        "status_reason": "rights verified; equipment confirmed",
        "original_sha256": "b" * 64,
        "evaluation_sha256": "c" * 64,
        "reviewer_role": "dataset-reviewer",
        "review_attestation": ("labels reviewed without access to any model output"),
    }
    entry.update(overrides)
    return entry


def test_no_tracked_private_files():
    """No private evidence may already be tracked.

    .gitignore does not protect tracked files, so the private
    register directory and the photos overlay must hold no tracked
    files other than committed placeholder .gitignore files.
    """
    result = _git("ls-files", "evals/realworld/private", "evals/realworld/photos")
    assert result.returncode == 0, result.stderr
    tracked = [
        line
        for line in result.stdout.splitlines()
        if line and not line.endswith(".gitignore")
    ]
    assert tracked == [], f"tracked private files must be removed: {tracked}"


def test_register_round_trip_tmp_path(tmp_path, monkeypatch):
    """A valid register saves and reloads from the private directory."""
    from audits.realworld_intake import load_register, save_register

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    register = {"register_version": "intake-v1", "entries": [_register_entry()]}
    path = save_register(register)
    assert path.parent == tmp_path / "priv"
    assert load_register() == register


def test_register_rejects_unexpected_fields(tmp_path, monkeypatch):
    """Private intake records reject unexpected fields."""
    from audits.realworld_intake import IntakeError, validate_register

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    entry = _register_entry(surprise_field="must fail")
    with pytest.raises(IntakeError):
        validate_register({"register_version": "intake-v1", "entries": [entry]})


def _manifest_case(**overrides):
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
            "sha256": "d" * 64,
            "provenance": "photographed by evaluator",
        },
        "ground_truth": {
            "equipment": "equipped",
            "visible_object": "vehicle-between-lanes button",
            "interpretation": "supports",
            "expected_state": "visually_supported",
            "verification_method": "window sticker on file",
            "ambiguity": "none noted",
        },
    }
    case.update(overrides)
    return case


def _manifest(cases):
    return {
        "manifest_version": "realworld-v1",
        "dataset": "intake-test",
        "split": "dev",
        "cases": cases,
    }


def test_value_scan_detects_planted_personal_data():
    """VINs, emails, phones, and data URIs in values are rejected."""
    from audits.realworld_intake import scan_values

    planted = {
        "provenance": "photo from 1HGCM82633A004352 lot",
        "verification_method": "owner jane.doe@example.com confirmed",
        "ambiguity": "call +1-555-014-2288 for access",
        "visible_object": "button data:image/png;base64,iVBORw0KGgo=",
    }
    for field, value in planted.items():
        with pytest.raises(ValueError):
            scan_values({field: value}, "case")


def test_value_scan_detects_buried_vin_with_word_boundaries():
    """A 17-char VIN-charset run inside prose is caught; short codes pass."""
    from audits.realworld_intake import scan_values

    with pytest.raises(ValueError):
        scan_values({"notes": "see 1hgcm82633a004352 in file"}, "case")
    scan_values({"notes": "trim EX, 16 wheels, 2021 model"}, "case")


def test_value_scan_ignores_sha256_and_ordinary_prose():
    """Digests and ordinary text are not flagged."""
    from audits.realworld_intake import scan_values

    scan_values(
        {"sha256": "e4f081b7623b6e3789a7ab41e34e335bd25d705332fe8d18fd5ff963da7d6441"},
        "photo",
    )
    scan_values(
        {"sha256": "E4F081B7623B6E3789A7AB41E34E335BD25D705332FE8D18FD5FF963DA7D6441"},
        "photo",
    )
    scan_values(
        {
            "provenance": "synthetic fixture generated for CI",
            "verification_method": "fixture by construction; no equipment claim",
            "ambiguity": "generic label cannot establish ACC capability",
        },
        "case",
    )


def test_value_scan_never_repeats_the_value():
    """Rejection messages name the field, never the suspicious value."""
    from audits.realworld_intake import scan_values

    secret = "1HGCM82633A004352"
    with pytest.raises(ValueError) as excinfo:
        scan_values({"provenance": f"vin {secret} here"}, "case")
    assert secret not in str(excinfo.value)


def test_validate_manifest_rejects_value_level_personal_data():
    """The public manifest validator enforces the value scan too."""
    from audits.realworld import validate_manifest

    case = _manifest_case()
    case["ground_truth"]["verification_method"] = "owner 1HGCM82633A004352 confirmed"
    with pytest.raises(ValueError):
        validate_manifest(_manifest([case]))


def test_existing_fixtures_pass_value_scan():
    """Synthetic fixtures and the example manifest validate unchanged."""
    from audits.realworld import validate_manifest

    for name in (
        "fixtures/realworld-sample/manifest.json",
        "evals/realworld/manifest.example.json",
    ):
        validate_manifest(REPO_ROOT / name)


def test_register_rejects_unknown_status_values(tmp_path, monkeypatch):
    """License/privacy/candidate vocabularies are closed."""
    from audits.realworld_intake import IntakeError, validate_register

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    for field, bad in [
        ("license_status", "maybe"),
        ("privacy_review", "skipped"),
        ("candidate_status", "archived"),
        ("claim_origin", "dealer"),
    ]:
        with pytest.raises(IntakeError):
            validate_register(
                {
                    "register_version": "intake-v1",
                    "entries": [_register_entry(**{field: bad})],
                }
            )
