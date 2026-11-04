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
        "evaluation_sha256": "d" * 64,
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


def test_value_scan_flags_strings_inside_lists():
    """VINs, emails, and phones in list items are flagged at any depth."""
    from audits.realworld_intake import scan_values

    with pytest.raises(ValueError):
        scan_values({"notes": ["see 1HGCM82633A004352 here"]}, "case")
    with pytest.raises(ValueError):
        scan_values({"contacts": [["jane.doe@example.com"]]}, "case")
    with pytest.raises(ValueError):
        scan_values({"history": [{"phone": "+1-555-014-2288"}]}, "case")
    with pytest.raises(ValueError):
        scan_values([["1hgcm82633a004352"]], "case")


def test_value_scan_passes_benign_lists_and_digests():
    """Ordinary list items and digest lists are not flagged."""
    from audits.realworld_intake import scan_values

    scan_values({"tags": ["steering", "wheel", "button", "2021 model"]}, "case")
    scan_values({"notes": []}, "case")
    scan_values(
        {
            "photo": {
                "sha256": "e4f081b7623b6e3789a7ab41e34e335bd25d705332fe8d18fd5ff963da7d6441"
            }
        },
        "case",
    )


def test_value_scan_list_error_hides_value():
    """List-item rejections name the position, never the value."""
    from audits.realworld_intake import scan_values

    secret = "1HGCM82633A004352"
    with pytest.raises(ValueError) as excinfo:
        scan_values({"notes": [f"vin {secret} here"]}, "case")
    assert secret not in str(excinfo.value)


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


def test_source_group_optional_in_public_schema():
    """realworld-v1 stays backward compatible: no group required."""
    from audits.realworld import validate_manifest

    cases = validate_manifest(_manifest([_manifest_case()]))
    assert cases[0].get("source_group") is None


def test_admission_requires_source_group():
    """A missing group on an accepted case is an admission failure."""
    from audits.realworld_intake import IntakeError, require_source_group

    with pytest.raises(IntakeError):
        require_source_group(_manifest_case(), "dev")
    require_source_group({**_manifest_case(), "source_group": "group-a1"}, "dev")


def test_cross_split_group_overlap_blocks():
    """Any source_group in both splits fails the cross-split check."""
    from audits.realworld_intake import IntakeError, check_cross_split

    dev = [{**_manifest_case(id="d1"), "source_group": "g1"}]
    holdout = [{**_manifest_case(id="h1"), "source_group": "g1"}]
    with pytest.raises(IntakeError):
        check_cross_split(dev, holdout)
    holdout_ok = [{**_manifest_case(id="h1"), "source_group": "g2"}]
    assert check_cross_split(dev, holdout_ok) == []


def test_exact_duplicate_hash_detected():
    """Identical SHA-256 across cases is reported with both case ids."""
    from audits.realworld_intake import IntakeError, check_exact_duplicates

    a = _manifest_case(id="a")
    b = _manifest_case(id="b")
    assert check_exact_duplicates([a, b]) == {"d" * 64: ["a", "b"]}
    with pytest.raises(IntakeError):
        check_exact_duplicates([a, b], strict=True)
    c = _manifest_case(id="c", photo={**_manifest_case()["photo"], "sha256": "e" * 64})
    assert check_exact_duplicates([a, c]) == {}


def test_near_duplicate_detection(tmp_path):
    """Near-identical images flag; distinct images do not."""
    from PIL import Image

    from audits.realworld_intake import NEAR_DUP_THRESHOLD, find_near_duplicates

    base = Image.new("RGB", (64, 64), (200, 30, 30))
    p1 = tmp_path / "p1.png"
    p2 = tmp_path / "p2.png"
    p3 = tmp_path / "p3.png"
    base.save(p1)
    slightly = base.copy()
    slightly.putpixel((0, 0), (201, 30, 30))
    slightly.save(p2)
    split = Image.new("RGB", (64, 64), (30, 30, 200))
    for x in range(32, 64):
        for y in range(64):
            split.putpixel((x, y), (230, 220, 40))
    split.save(p3)
    hits = find_near_duplicates([("p1", p1), ("p2", p2), ("p3", p3)])
    pairs = {(h[0], h[1]) for h in hits}
    assert ("p1", "p2") in pairs
    assert all("p3" not in pair for pair in pairs)
    assert all(h[2] <= NEAR_DUP_THRESHOLD for h in hits)


def _exif_jpeg(path, *, orientation=None, gps=False):
    from PIL import Image

    img = Image.new("RGB", (64, 32), (200, 30, 30))
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    if gps:
        exif[34853] = {
            0: (2, 3, 0, 0),
            1: "N",
            2: (51, 1, 30, 1),
            3: "W",
            4: (0, 1, 0, 1),
        }
    img.save(path, "JPEG", exif=exif)


def test_evaluation_copy_preserves_png_pixels_and_strips_metadata(tmp_path):
    """A plain PNG yields pixel-identical output with no EXIF."""
    from PIL import Image, ImageChops

    from audits.realworld_intake import inspect_image, prepare_evaluation_copy

    src = tmp_path / "orig.png"
    Image.new("RGB", (64, 32), (200, 30, 30)).save(src)
    dest = tmp_path / "eval.png"
    report = prepare_evaluation_copy(src, dest)
    assert report["orientation"] is None
    assert report["transform"] == "none"
    assert report["gps_present"] is False
    assert ImageChops.difference(Image.open(src), Image.open(dest)).getbbox() is None
    assert inspect_image(dest)["orientation"] is None


def test_evaluation_copy_bakes_exif_rotation_and_records_it(tmp_path):
    """EXIF orientation is recorded first, then baked in as pixels."""
    from PIL import Image

    from audits.realworld_intake import inspect_image, prepare_evaluation_copy

    src = tmp_path / "rot.jpg"
    _exif_jpeg(src, orientation=6)
    assert inspect_image(src)["orientation"] == 6
    dest = tmp_path / "eval.jpg"
    report = prepare_evaluation_copy(src, dest)
    assert report["orientation"] == 6
    assert report["transform"] == "exif-transpose"
    out = Image.open(dest)
    assert out.size == (32, 64)
    assert inspect_image(dest)["orientation"] is None


def test_none_transform_verifies_pixel_identity(tmp_path, monkeypatch):
    """A corrupted 'none' copy is caught and the derived file removed."""
    from PIL import Image

    from audits import realworld_intake as intake
    from audits.realworld_intake import IntakeError

    src = tmp_path / "orig.png"
    Image.new("RGB", (16, 16), (10, 10, 10)).save(src)
    real_save = Image.Image.save

    def corrupting_save(self, fp, **kwargs):
        real_save(self, fp, **kwargs)
        if str(fp).endswith("eval.png"):
            altered = Image.open(fp)
            altered.putpixel((0, 0), (0, 0, 0))
            real_save(altered, fp)

    monkeypatch.setattr(Image.Image, "save", corrupting_save)
    with pytest.raises(IntakeError, match="pixel identity"):
        intake.prepare_evaluation_copy(src, tmp_path / "eval.png")
    assert not (tmp_path / "eval.png").exists()


def test_webp_takes_reencode_path(tmp_path):
    """WEBP is allowed through the documented lossy path."""
    from PIL import Image

    from audits import realworld_intake as intake

    src = tmp_path / "orig.webp"
    Image.new("RGB", (16, 16), (10, 200, 10)).save(src, "WEBP")
    report = intake.prepare_evaluation_copy(src, tmp_path / "eval.webp")
    assert report["transform"] == "re-encode"
    assert report["format"] == "WEBP"


def test_unsupported_format_rejected(tmp_path):
    """Formats outside the app allowlist are refused with guidance."""
    from PIL import Image

    from audits import realworld_intake as intake
    from audits.realworld_intake import IntakeError

    src = tmp_path / "orig.gif"
    Image.new("RGB", (16, 16), (10, 10, 10)).save(src, "GIF")
    with pytest.raises(IntakeError, match="convert"):
        intake.prepare_evaluation_copy(src, tmp_path / "eval.gif")


def test_gps_metadata_quarantines_photo(tmp_path):
    """Images carrying GPS metadata are refused, not silently stripped."""

    from audits.realworld_intake import IntakeError, prepare_evaluation_copy

    src = tmp_path / "gps.jpg"
    _exif_jpeg(src, gps=True)
    with pytest.raises(IntakeError):
        prepare_evaluation_copy(src, tmp_path / "eval.jpg")


def test_evaluation_copy_refuses_in_place_and_missing(tmp_path):
    """Never modify an admitted copy in place; missing files fail."""
    from PIL import Image

    from audits.realworld_intake import IntakeError, prepare_evaluation_copy

    src = tmp_path / "orig.png"
    Image.new("RGB", (16, 16), (10, 10, 10)).save(src)
    with pytest.raises(IntakeError):
        prepare_evaluation_copy(src, src)
    with pytest.raises(IntakeError):
        prepare_evaluation_copy(tmp_path / "absent.png", tmp_path / "eval.png")


def test_evaluation_sha_computed_after_final(tmp_path):
    """Manifest digest matches the exact bytes of the final copy."""
    import hashlib

    from PIL import Image

    from audits.realworld_intake import prepare_evaluation_copy

    src = tmp_path / "orig.png"
    Image.new("RGB", (16, 16), (10, 200, 10)).save(src)
    dest = tmp_path / "eval.png"
    report = prepare_evaluation_copy(src, dest)
    assert report["evaluation_sha256"] == hashlib.sha256(dest.read_bytes()).hexdigest()
    assert report["original_sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()


def _admission_pair(**case_overrides):
    entry = _register_entry()
    case = _manifest_case()
    case["source_group"] = "group-a1b2c3"
    case.update(case_overrides)
    return entry, case


def test_admission_accepts_complete_record():
    """A fully evidenced photo-only case is admitted."""
    from audits.realworld_intake import validate_admission

    entry, case = _admission_pair()
    assert validate_admission(entry, case) == "accepted"


def test_admission_rejects_verified_and_contradicted_states():
    """Photo-only pilot manifests cannot promise verified/contradicted."""
    from audits.realworld_intake import IntakeError, validate_admission

    for state in ("verified", "contradicted"):
        entry, case = _admission_pair()
        case["ground_truth"]["expected_state"] = state
        with pytest.raises(IntakeError):
            validate_admission(entry, case)


def test_admission_accepts_non_authoritative_states():
    """insufficient_evidence / visually_supported / not_visually_verifiable pass."""
    from audits.realworld_intake import validate_admission

    for state in (
        "insufficient_evidence",
        "visually_supported",
        "not_visually_verifiable",
    ):
        entry, case = _admission_pair()
        case["ground_truth"]["expected_state"] = state
        assert validate_admission(entry, case) == "accepted"


def test_admission_requires_rights_privacy_equipment():
    """Pending license, failed privacy, or missing equipment blocks admission."""
    from audits.realworld_intake import IntakeError, validate_admission

    entry, case = _admission_pair()
    for field, bad in [
        ("license_status", "pending"),
        ("license_status", "rejected"),
        ("privacy_review", "pending"),
        ("privacy_review", "failed"),
        ("candidate_status", "pending"),
        ("equipment_method", ""),
        ("equipment_evidence", ""),
    ]:
        with pytest.raises(IntakeError):
            validate_admission({**entry, field: bad}, case)


def test_admission_rejects_hash_mismatch():
    """Register evaluation hash must equal the case photo hash."""
    from audits.realworld_intake import IntakeError, validate_admission

    entry, case = _admission_pair()
    entry["evaluation_sha256"] = "e" * 64
    with pytest.raises(IntakeError) as excinfo:
        validate_admission(entry, case)
    assert "evaluation_sha256" in str(excinfo.value)
    assert "e" * 64 not in str(excinfo.value)


def test_admission_rejects_missing_source_group():
    """A case with no (or empty) source_group fails the explicit gate."""
    from audits.realworld_intake import IntakeError, validate_admission

    entry, case = _admission_pair()
    case.pop("source_group")
    with pytest.raises(IntakeError, match="has no source_group"):
        validate_admission(entry, case)
    entry, case = _admission_pair()
    case["source_group"] = ""
    with pytest.raises(IntakeError, match="has no source_group"):
        validate_admission(entry, case)


def test_admission_rejects_group_mismatch():
    """Register and case source groups must be identical (opaque)."""
    from audits.realworld_intake import IntakeError, validate_admission

    entry, case = _admission_pair()
    entry["source_group"] = "group-other"
    with pytest.raises(IntakeError) as excinfo:
        validate_admission(entry, case)
    assert "source_group" in str(excinfo.value)
    assert "group-other" not in str(excinfo.value)


def test_admission_rejects_vehicle_mismatch():
    """Register vehicle must match the case vehicle field by field."""
    from audits.realworld_intake import IntakeError, validate_admission

    entry, case = _admission_pair()
    entry["vehicle"] = {**entry["vehicle"], "model": "Optima"}
    with pytest.raises(IntakeError) as excinfo:
        validate_admission(entry, case)
    assert "vehicle" in str(excinfo.value)
    assert "Optima" not in str(excinfo.value)


def test_admission_vehicle_compare_ignores_case_and_spacing():
    """Make/model/trim/market compare after strip+casefold; year as string."""
    from audits.realworld_intake import validate_admission

    entry, case = _admission_pair()
    entry["vehicle"] = {
        "make": "  kia ",
        "model": "FORTE",
        "year": "2021",
        "trim": "gt-line",
        "market": " us",
    }
    assert validate_admission(entry, case) == "accepted"


def test_admission_requires_matching_claim_text():
    """Register claim text must equal the manifest's seller wording."""
    from audits.realworld_intake import IntakeError, validate_admission

    entry, case = _admission_pair()
    case["claim"]["advertised"] = "Adaptive Cruise Control with Stop & Go"
    with pytest.raises(IntakeError):
        validate_admission(entry, case)


def test_not_equipped_positive_prediction_scores_hallucinated():
    """A visually_supported prediction on a not-equipped case is penalized."""
    from audits.realworld import score_case

    _entry, case = _admission_pair()
    case["ground_truth"]["equipment"] = "not-equipped"
    case["ground_truth"]["interpretation"] = "generic"
    case["ground_truth"]["expected_state"] = "insufficient_evidence"
    row = score_case(
        case, {"mode": "baseline", "status": "scored", "state": "visually_supported"}
    )
    assert row["hallucinated_positive"] is True
    assert row["state_correct"] is False


def test_correct_insufficient_prediction_scores_clean():
    """insufficient_evidence predicted on a not-equipped case is correct."""
    from audits.realworld import score_case

    _entry, case = _admission_pair()
    case["ground_truth"]["equipment"] = "not-equipped"
    case["ground_truth"]["expected_state"] = "insufficient_evidence"
    row = score_case(
        case, {"mode": "baseline", "status": "scored", "state": "insufficient_evidence"}
    )
    assert row["state_correct"] is True
    assert row["hallucinated_positive"] is False


def _photo_manifest(ids, digest="f" * 64):
    return _manifest(
        [
            {
                **_manifest_case(id=cid),
                "source_group": f"group-{cid}",
                "photo": {
                    "path": f"photos/{cid}.png",
                    "sha256": digest,
                    "provenance": "evaluator photograph",
                },
            }
            for cid in ids
        ]
    )


def _write_case_photo(tmp_path, name, color=(200, 30, 30)):
    import hashlib

    from PIL import Image

    path = tmp_path / "photos" / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _freeze_pair(tmp_path, *, holdout_ids=("h1", "h2"), dev_ids=("d1",)):
    """Real-file dev/holdout manifests with matching hashes for freeze tests."""
    holdout_cases = []
    dev_cases = []
    photo_paths = {}
    for cid in holdout_ids:
        path, digest = _write_case_photo(tmp_path / "hold", cid)
        holdout_cases.append(
            {
                **_manifest_case(id=cid),
                "source_group": f"group-{cid}",
                "photo": {
                    "path": f"{cid}.png",
                    "sha256": digest,
                    "provenance": "evaluator photograph",
                },
            }
        )
        photo_paths[cid] = path
    for cid in dev_ids:
        import hashlib as _hashlib

        from PIL import Image as _Image

        dev_path = tmp_path / "dev" / "photos" / f"{cid}.png"
        dev_path.parent.mkdir(parents=True, exist_ok=True)
        split = _Image.new("RGB", (32, 32), (30, 30, 200))
        for x in range(16, 32):
            for y in range(32):
                split.putpixel((x, y), (230, 220, 40))
        split.save(dev_path)
        digest = _hashlib.sha256(dev_path.read_bytes()).hexdigest()
        dev_cases.append(
            {
                **_manifest_case(id=cid),
                "source_group": f"group-{cid}",
                "photo": {
                    "path": f"{cid}.png",
                    "sha256": digest,
                    "provenance": "evaluator photograph",
                },
            }
        )
        photo_paths[cid] = dev_path
    holdout = {
        "manifest_version": "realworld-v1",
        "dataset": "pilot",
        "split": "holdout",
        "cases": holdout_cases,
    }
    dev = {
        "manifest_version": "realworld-v1",
        "dataset": "pilot",
        "split": "dev",
        "cases": dev_cases,
    }
    return holdout, dev, photo_paths


def test_freeze_refuses_without_dev_manifest(tmp_path, monkeypatch):
    """A freeze with no dev manifest (or no photo paths) cannot proceed."""
    from audits.realworld_intake import IntakeError, freeze_holdout

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    holdout, dev, photo_paths = _freeze_pair(tmp_path)
    with pytest.raises(IntakeError):
        freeze_holdout(
            dataset="pilot",
            manifest=holdout,
            dev_manifest=None,
            photo_paths=photo_paths,
            reviewer_role="dataset-reviewer",
        )
    with pytest.raises(IntakeError):
        freeze_holdout(
            dataset="pilot",
            manifest=holdout,
            dev_manifest=dev,
            photo_paths=None,
            reviewer_role="dataset-reviewer",
        )


def test_freeze_refuses_incomplete_photo_coverage(tmp_path, monkeypatch):
    """Every case in both manifests needs a photo path."""
    from audits.realworld_intake import IntakeError, freeze_holdout

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    holdout, dev, photo_paths = _freeze_pair(tmp_path)
    photo_paths.pop("h1")
    with pytest.raises(IntakeError) as excinfo:
        freeze_holdout(
            dataset="pilot",
            manifest=holdout,
            dev_manifest=dev,
            photo_paths=photo_paths,
            reviewer_role="dataset-reviewer",
        )
    assert "h1" in str(excinfo.value)


def test_freeze_refuses_wrong_splits(tmp_path, monkeypatch):
    """Only a holdout manifest against a dev manifest may freeze."""
    from audits.realworld_intake import IntakeError, freeze_holdout

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    holdout, dev, photo_paths = _freeze_pair(tmp_path)
    dev_split = {**holdout, "split": "dev"}
    with pytest.raises(IntakeError):
        freeze_holdout(
            dataset="pilot",
            manifest=dev_split,
            dev_manifest=dev,
            photo_paths=photo_paths,
            reviewer_role="dataset-reviewer",
        )
    holdout_dev = {**dev, "split": "holdout"}
    with pytest.raises(IntakeError):
        freeze_holdout(
            dataset="pilot",
            manifest=holdout,
            dev_manifest=holdout_dev,
            photo_paths=photo_paths,
            reviewer_role="dataset-reviewer",
        )


def test_freeze_refuses_sha_mismatch(tmp_path, monkeypatch):
    """Bytes that do not match the manifest digest block the freeze."""
    from audits.realworld_intake import IntakeError, freeze_holdout

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    holdout, dev, photo_paths = _freeze_pair(tmp_path)
    holdout["cases"][0]["photo"]["sha256"] = "0" * 64
    with pytest.raises(IntakeError) as excinfo:
        freeze_holdout(
            dataset="pilot",
            manifest=holdout,
            dev_manifest=dev,
            photo_paths=photo_paths,
            reviewer_role="dataset-reviewer",
        )
    assert "sha256" in str(excinfo.value)


def test_freeze_refuses_overwrite_without_reason(tmp_path, monkeypatch):
    """An existing freeze is never silently overwritten."""
    from audits.realworld_intake import IntakeError, freeze_holdout

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    holdout, dev, photo_paths = _freeze_pair(tmp_path)
    first = freeze_holdout(
        dataset="pilot",
        manifest=holdout,
        dev_manifest=dev,
        photo_paths=photo_paths,
        reviewer_role="dataset-reviewer",
    )
    with pytest.raises(IntakeError):
        freeze_holdout(
            dataset="pilot",
            manifest=holdout,
            dev_manifest=dev,
            photo_paths=photo_paths,
            reviewer_role="dataset-reviewer",
        )
    assert first["manifest_sha256"]


def test_freeze_replace_archives_prior_record(tmp_path, monkeypatch):
    """A recorded reason archives the prior freeze and links supersession."""
    from audits.realworld_intake import check_holdout_freeze, freeze_holdout

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    holdout, dev, photo_paths = _freeze_pair(tmp_path)
    first = freeze_holdout(
        dataset="pilot",
        manifest=holdout,
        dev_manifest=dev,
        photo_paths=photo_paths,
        reviewer_role="dataset-reviewer",
    )
    second = freeze_holdout(
        dataset="pilot",
        manifest=holdout,
        dev_manifest=dev,
        photo_paths=photo_paths,
        reviewer_role="dataset-reviewer",
        replace_reason="re-freeze after adding reviewer role",
    )
    assert second["supersedes"] == first["manifest_sha256"]
    assert second["replace_reason"] == "re-freeze after adding reviewer role"
    archived = list((tmp_path / "priv").glob("holdout-freeze-pilot.superseded-*.json"))
    assert len(archived) == 1
    assert (
        check_holdout_freeze("pilot", holdout)["manifest_sha256"]
        == second["manifest_sha256"]
    )


def test_holdout_freeze_round_trip(tmp_path, monkeypatch):
    """Freeze records manifest hash + inventory; unchanged check passes."""
    from audits.realworld_intake import check_holdout_freeze, freeze_holdout

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    manifest, dev, photo_paths = _freeze_pair(tmp_path)
    record = freeze_holdout(
        dataset="pilot",
        manifest=manifest,
        dev_manifest=dev,
        photo_paths=photo_paths,
        reviewer_role="dataset-reviewer",
    )
    assert record["manifest_sha256"]
    assert record["case_ids"] == ["h1", "h2"]
    assert (
        check_holdout_freeze("pilot", manifest)["manifest_sha256"]
        == record["manifest_sha256"]
    )


def test_holdout_change_detected(tmp_path, monkeypatch):
    """A changed holdout manifest fails the freeze check."""
    from audits.realworld_intake import (
        IntakeError,
        check_holdout_freeze,
        freeze_holdout,
    )

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    manifest, dev, photo_paths = _freeze_pair(tmp_path)
    freeze_holdout(
        dataset="pilot",
        manifest=manifest,
        dev_manifest=dev,
        photo_paths=photo_paths,
        reviewer_role="dataset-reviewer",
    )
    tampered = _photo_manifest(["h1", "h2", "h3"])
    with pytest.raises(IntakeError):
        check_holdout_freeze("pilot", tampered)


def test_missing_freeze_record_reported(tmp_path, monkeypatch):
    """No freeze record is an explicit failure, never a silent pass."""
    from audits.realworld_intake import IntakeError, check_holdout_freeze

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    with pytest.raises(IntakeError):
        check_holdout_freeze("pilot", _photo_manifest(["h1"]))


def _split_case(cid, group, photo_name, digest):
    return {
        **_manifest_case(id=cid),
        "source_group": group,
        "photo": {
            "path": photo_name,
            "sha256": digest,
            "provenance": "evaluator photograph",
        },
    }


def _split_manifest(split, cases):
    return {
        "manifest_version": "realworld-v1",
        "dataset": "pilot",
        "split": split,
        "cases": cases,
    }


def test_duplicates_block_freeze_until_disposition(tmp_path, monkeypatch):
    """Exact and near duplicates across splits block the freeze."""
    import hashlib
    import shutil

    from PIL import Image

    from audits.realworld_intake import IntakeError, freeze_holdout

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    red = tmp_path / "red.png"
    Image.new("RGB", (32, 32), (200, 30, 30)).save(red)
    red_sha = hashlib.sha256(red.read_bytes()).hexdigest()
    dev = _split_manifest("dev", [_split_case("d1", "group-d1", "red.png", red_sha)])
    same = tmp_path / "same.png"
    shutil.copy(red, same)
    holdout = _split_manifest(
        "holdout", [_split_case("h1", "group-h1", "same.png", red_sha)]
    )
    photos = {"d1": red, "h1": same}
    with pytest.raises(IntakeError):
        freeze_holdout(
            dataset="pilot",
            manifest=holdout,
            dev_manifest=dev,
            photo_paths=photos,
            reviewer_role="dataset-reviewer",
        )

    near = tmp_path / "near.png"
    altered = Image.open(red)
    altered.putpixel((0, 0), (201, 30, 30))
    altered.save(near)
    near_sha = hashlib.sha256(near.read_bytes()).hexdigest()
    distinct = _split_manifest(
        "holdout", [_split_case("h1", "group-h1", "near.png", near_sha)]
    )
    photos = {"d1": red, "h1": near}
    with pytest.raises(IntakeError):
        freeze_holdout(
            dataset="pilot",
            manifest=distinct,
            dev_manifest=dev,
            photo_paths=photos,
            reviewer_role="dataset-reviewer",
        )
    record = freeze_holdout(
        dataset="pilot",
        manifest=distinct,
        dev_manifest=dev,
        photo_paths=photos,
        reviewer_role="dataset-reviewer",
        dispositions={"d1|h1": "distinct vehicles confirmed by dataset-reviewer"},
    )
    assert record["dispositions"] == {
        "d1|h1": "distinct vehicles confirmed by dataset-reviewer"
    }


def test_holdout_evaluation_log_is_manual(tmp_path, monkeypatch):
    """Authorized evaluations append a manual log entry with full context."""
    from audits.realworld_intake import log_holdout_evaluation, read_evaluation_log

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    entry = log_holdout_evaluation(
        dataset="pilot",
        manifest_hash="ab" * 32,
        prompt_versions={"muse": "m2", "sam": "s1"},
        run_date="2026-11-04",
        reason="pilot smoke check",
    )
    assert entry["reason"] == "pilot smoke check"
    assert read_evaluation_log("pilot") == [entry]


def _quality_inputs():
    entries = [
        _register_entry(id="case-zz-1", candidate_status="accepted"),
        _register_entry(
            id="case-zz-2", candidate_status="pending", license_status="pending"
        ),
        _register_entry(
            id="case-zz-3", candidate_status="rejected", privacy_review="failed"
        ),
    ]
    dev = [_photo_manifest(["case-zz-1"])["cases"][0]]
    holdout = [_photo_manifest(["case-zz-2"])["cases"][0]]
    return entries, dev, holdout


def test_private_report_counts_and_lists():
    """Private report carries counts plus configuration and finding detail."""
    from audits.realworld_intake import build_quality_report

    entries, dev, holdout = _quality_inputs()
    report = build_quality_report(entries=entries, dev_cases=dev, holdout_cases=holdout)
    assert report["counts"] == {"accepted": 1, "pending": 1, "rejected": 1}
    assert report["pending_counted_as_accepted"] is False
    assert len(report["configurations"]) == 1
    assert report["configurations"][0]["model"] == "Forte"
    assert report["freeze_status"] == "missing"


def test_public_report_is_counts_only():
    """Public aggregate exposes no makes, models, years, trims, or ids."""
    import json

    from audits.realworld_intake import build_quality_report, public_report

    entries, dev, holdout = _quality_inputs()
    private = build_quality_report(
        entries=entries, dev_cases=dev, holdout_cases=holdout
    )
    public = public_report(private)
    assert public["counts"] == {"accepted": 1, "pending": 1, "rejected": 1}
    blob = json.dumps(public)
    for secret in (
        "Forte",
        "Kia",
        "2021",
        "GT-Line",
        "case-zz-1",
        "case-zz-2",
        "group-",
    ):
        assert secret not in blob, secret
    assert public["freeze_status"] == "missing"


def test_public_report_uses_not_measured():
    """Empty splits and unknown freeze state report as not measured."""
    from audits.realworld_intake import build_quality_report, public_report

    entries, _, _ = _quality_inputs()
    private = build_quality_report(entries=entries, dev_cases=[], holdout_cases=[])
    public = public_report(private)
    assert public["dev"]["n"] == "not measured"
    assert public["claim_origin"]["seller"] == 3


def test_validate_intake_passes_on_synthetic_fixtures():
    """The full intake validator runs clean on the synthetic fixtures."""
    from audits.realworld_intake import validate_intake

    report = validate_intake(
        manifest=REPO_ROOT / "fixtures" / "realworld-sample" / "manifest.json",
        photos_root=REPO_ROOT / "fixtures" / "realworld-sample",
    )
    assert report["passed"] is True, report["failures"]
    assert report["public_report"]["counts"] == {
        "accepted": 0,
        "pending": 0,
        "rejected": 0,
    }


def test_validate_intake_surfaces_admission_failure():
    """A pending license in the linked register fails the run."""
    from audits.realworld_intake import validate_intake

    manifest = _photo_manifest(["case-zz-1"], digest="d" * 64)
    manifest["cases"][0]["source_group"] = "group-a1b2c3"
    entry = _register_entry(id="case-zz-1", license_status="pending")
    report = validate_intake(manifest=manifest, entries={"case-zz-1": entry})
    assert report["passed"] is False
    assert any("rights" in failure for failure in report["failures"])


def test_validate_intake_rejects_photo_only_forbidden_states():
    """verified/contradicted labels fail even without a register."""
    from audits.realworld_intake import validate_intake

    manifest = _photo_manifest(["case-zz-1"])
    manifest["cases"][0]["ground_truth"]["expected_state"] = "verified"
    report = validate_intake(manifest=manifest)
    assert report["passed"] is False
    assert any("photo-only" in failure for failure in report["failures"])


def test_validate_intake_enforces_holdout_freeze(tmp_path, monkeypatch):
    """Frozen holdouts pass; tampered ones fail; missing needs the flag."""
    from audits.realworld_intake import validate_intake

    monkeypatch.setenv("VCC_REALWORLD_PRIVATE_DIR", str(tmp_path / "priv"))
    manifest = _photo_manifest(["h1"])
    manifest["split"] = "holdout"
    manifest["dataset"] = "pilot"
    soft = validate_intake(manifest=manifest)
    assert soft["passed"] is True
    assert soft["freeze"]["status"] == "missing"
    hard = validate_intake(manifest=manifest, enforce_freeze=True)
    assert hard["passed"] is False


def test_validate_intake_detects_tampered_photo_bytes(tmp_path):
    """SHA-256 verification against the photos root catches corruption."""
    import json
    import shutil

    from audits.realworld_intake import validate_intake

    shutil.copytree(REPO_ROOT / "fixtures" / "realworld-sample", tmp_path / "sample")
    manifest_path = tmp_path / "sample" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert (
        validate_intake(manifest=manifest, photos_root=tmp_path / "sample")["passed"]
        is True
    )
    with open(tmp_path / "sample" / "photos" / "synth-acc-001.png", "ab") as handle:
        handle.write(b"tampered")
    report = validate_intake(manifest=manifest, photos_root=tmp_path / "sample")
    assert report["passed"] is False
    assert any("sha256 mismatch" in failure for failure in report["failures"])


def _near_dupe_pair(tmp_path):
    """Dev/holdout manifests whose photos differ by one pixel."""
    import hashlib

    from PIL import Image

    hold_dir = tmp_path / "hold"
    dev_dir = tmp_path / "devp"
    hold_dir.mkdir(parents=True, exist_ok=True)
    dev_dir.mkdir(parents=True, exist_ok=True)
    red = hold_dir / "h.png"
    Image.new("RGB", (32, 32), (200, 30, 30)).save(red)
    near = dev_dir / "d.png"
    altered = Image.open(red)
    altered.putpixel((0, 0), (201, 30, 30))
    altered.save(near)

    def manifest(split, cid, group, path):
        return {
            "manifest_version": "realworld-v1",
            "dataset": "pilot",
            "split": split,
            "cases": [
                {
                    **_manifest_case(id=cid),
                    "source_group": group,
                    "photo": {
                        "path": path.name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "provenance": "evaluator photograph",
                    },
                }
            ],
        }

    return (
        manifest("holdout", "h1", "group-h1", red),
        manifest("dev", "d1", "group-d1", near),
        hold_dir,
        dev_dir,
    )


def test_validate_flags_cross_split_near_duplicate(tmp_path):
    """An unresolved cross-split near-duplicate fails validation."""
    from audits.realworld_intake import validate_intake

    holdout, dev, hold_dir, dev_dir = _near_dupe_pair(tmp_path)
    report = validate_intake(
        manifest=holdout,
        photos_root=hold_dir,
        other_manifest=dev,
        other_photos_root=dev_dir,
    )
    assert report["passed"] is False
    assert any("near-duplicate" in failure for failure in report["failures"])


def test_validate_passes_cross_split_near_duplicate_with_disposition(tmp_path):
    """A recorded disposition resolves the cross-split near-duplicate."""
    from audits.realworld_intake import validate_intake

    holdout, dev, hold_dir, dev_dir = _near_dupe_pair(tmp_path)
    report = validate_intake(
        manifest=holdout,
        photos_root=hold_dir,
        other_manifest=dev,
        other_photos_root=dev_dir,
        dispositions={"d1|h1": "distinct vehicles confirmed by dataset-reviewer"},
    )
    assert report["passed"] is True, report["failures"]


def test_validate_intake_public_branch_has_no_identifiers():
    """The public branch of the intake report stays counts-only."""
    import json

    from audits.realworld_intake import validate_intake

    manifest = _photo_manifest(["case-zz-1"])
    report = validate_intake(manifest=manifest)
    blob = json.dumps(report["public_report"])
    for secret in ("case-zz-1", "group-", "Forte", "Kia"):
        assert secret not in blob, secret


def _candidate_source(**overrides):
    source = {
        "id": "cand-forte-2015-dash",
        "source_group": "group-c1a2b3",
        "file_page_url": "https://commons.wikimedia.org/wiki/File:2015_Kia_Forte_06.jpg",
        "file_url": "https://upload.wikimedia.org/wikipedia/commons/3/3e/2015_Kia_Forte_06.jpg",
        "rights_holder": "CapCase (Flickr James Case stream, via Commons UploadWizard)",
        "license": "CC BY 2.0",
        "license_url": "https://creativecommons.org/licenses/by/2.0",
        "license_verified": True,
        "vehicle": {
            "make": "Kia",
            "model": "Forte",
            "year": 2015,
            "trim": "unstated",
            "market": "unstated",
        },
        "visible_control_note": "categories state steering wheel and dashboard; exact controls unconfirmed without visual inspection",
        "claim_text": "Lane Keeping Assist",
        "claim_origin": "researcher",
        "equipment_evidence_needed": "window sticker, build sheet, or verified equipment record for this vehicle",
        "privacy_note": "plates and EXIF unknown until bytes are inspected under a privacy review",
        "image_bytes": "not downloaded",
        "candidate_status": "pending",
        "status_reason": "license verified from Commons metadata; equipment and privacy evidence outstanding",
        "date_researched": "2026-11-04",
    }
    source.update(overrides)
    return source


def test_candidate_sources_validate():
    """A well-formed public-metadata candidate list passes."""
    from audits.realworld_intake import validate_candidate_sources

    doc = {"candidate_version": "candidates-v1", "sources": [_candidate_source()]}
    assert validate_candidate_sources(doc) == doc


def test_candidate_sources_reject_unknown_fields_and_bad_license():
    """Candidate records reject unknown fields and unverified licenses."""
    from audits.realworld_intake import IntakeError, validate_candidate_sources

    with pytest.raises(IntakeError):
        validate_candidate_sources(
            {
                "candidate_version": "candidates-v1",
                "sources": [_candidate_source(surprise=1)],
            }
        )
    with pytest.raises(IntakeError):
        validate_candidate_sources(
            {
                "candidate_version": "candidates-v1",
                "sources": [
                    _candidate_source(
                        license="all rights reserved", license_verified=False
                    )
                ],
            }
        )


def test_candidate_sources_never_accepted_without_phase_c():
    """Phase B research never marks a candidate accepted."""
    from audits.realworld_intake import IntakeError, validate_candidate_sources

    with pytest.raises(IntakeError):
        validate_candidate_sources(
            {
                "candidate_version": "candidates-v1",
                "sources": [_candidate_source(candidate_status="accepted")],
            }
        )


def test_candidate_sources_contain_no_image_bytes():
    """The research register holds pointers only, never image content."""
    import base64

    from audits.realworld_intake import validate_candidate_sources

    doc = {"candidate_version": "candidates-v1", "sources": [_candidate_source()]}
    validate_candidate_sources(doc)
    for source in doc["sources"]:
        for key, value in source.items():
            if not isinstance(value, str):
                continue
            assert not value.startswith("data:"), key
            if len(value) > 1000:
                try:
                    base64.b64decode(value, validate=True)
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"{key} looks like embedded bytes")


def _ready_source(**overrides):
    source = _candidate_source(id="cand-ready-001")
    source.update(
        {
            "rights_confirmed": True,
            "privacy_review": "passed",
            "equipment_evidence": "window sticker record 2026-11-05 on file",
            "original_sha256": "b" * 64,
            "evaluation_sha256": "c" * 64,
        }
    )
    source.update(overrides)
    return source


def test_readiness_blocks_evidence_free_candidate():
    """A metadata-only candidate is blocked on privacy, equipment, hashes."""
    from audits.realworld_intake import candidate_readiness

    report = candidate_readiness(_candidate_source())
    assert report["ready"] is False
    assert "manual privacy review of actual bytes" in report["missing"]
    assert "vehicle-specific equipment evidence" in report["missing"]
    assert "original_sha256" in report["missing"]
    assert "evaluation_sha256" in report["missing"]


def test_readiness_passes_fully_evidenced_candidate():
    """All Phase C evidence present admits readiness."""
    from audits.realworld_intake import candidate_readiness

    report = candidate_readiness(_ready_source())
    assert report == {"id": "cand-ready-001", "ready": True, "missing": []}


def test_readiness_requires_reusable_license_and_confirmation():
    """Unverifiable licenses and unconfirmed rights block readiness."""
    from audits.realworld_intake import candidate_readiness

    assert (
        "usage rights verified for evaluation use"
        in candidate_readiness(
            _ready_source(license="unknown", license_verified=False)
        )["missing"]
    )
    assert (
        "rights-holder confirmation for this evaluation"
        in candidate_readiness(_ready_source(rights_confirmed=False))["missing"]
    )


def test_phase_c_audit_summarizes_blocked_candidates():
    """The audit separates ready from blocked with exact shortfalls."""
    from audits.realworld_intake import audit_phase_c_readiness

    doc = {
        "candidate_version": "candidates-v1",
        "sources": [_candidate_source(), _ready_source()],
    }
    audit = audit_phase_c_readiness(doc)
    assert audit["summary"] == {"ready": 1, "blocked": 1, "total": 2}
    assert audit["ready"] == ["cand-ready-001"]
    assert audit["blocked"][0]["id"] == "cand-forte-2015-dash"
    assert audit["blocked"][0]["missing"]


def test_validate_print_says_not_measured_without_register():
    """No register: the printed line admits admission was not measured."""
    import json

    from audits.realworld_intake import format_validate_output, validate_intake

    manifest = json.loads(
        (REPO_ROOT / "fixtures" / "realworld-sample" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    report = validate_intake(
        manifest=manifest, photos_root=REPO_ROOT / "fixtures" / "realworld-sample"
    )
    text = format_validate_output(report)
    assert "passed=True" in text
    assert "admission=not measured" in text
    assert "NOT a completed dataset" in text
    assert "rights, privacy, and equipment evidence are unverified" in text


def test_validate_print_says_measured_with_register():
    """Linked register: the printed line says admission was measured."""
    from audits.realworld_intake import format_validate_output, validate_intake

    manifest = _photo_manifest(["case-zz-1"], digest="d" * 64)
    manifest["cases"][0]["source_group"] = "group-a1b2c3"
    entry = _register_entry(id="case-zz-1")
    report = validate_intake(manifest=manifest, entries={"case-zz-1": entry})
    text = format_validate_output(report)
    assert "admission=measured" in text
    assert "NOT a completed dataset" not in text


def test_intake_never_derives_labels_from_reconciliation():
    """Admission code must not call the production reconciler."""
    source = (REPO_ROOT / "backend" / "audits" / "realworld_intake.py").read_text(
        encoding="utf-8"
    )
    assert "reconcile_claim" not in source


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
