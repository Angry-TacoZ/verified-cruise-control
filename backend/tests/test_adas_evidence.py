"""Layer A/D: feature normalization, catalog schema, control matching.

Pure unit tests (no database, no providers, no inference): the mapping
from observed controls to supported capabilities must be deterministic
and must never invent equipment knowledge.
"""

from __future__ import annotations

import json

import pytest

from audits import adas_evidence
from audits.adas_evidence import (
    Catalog,
    interpret_control,
    load_catalog,
    match_entry,
    normalize_feature,
)


@pytest.fixture()
def catalog() -> Catalog:
    return load_catalog()


def test_catalog_loads_with_provenance(catalog):
    assert catalog.version == "2026-11-01/v1"
    assert len(catalog.entries) == 6
    for entry in catalog.entries:
        assert entry.sources, entry.id
        assert entry.certainty in ("confirmed", "provisional", "definitional")
        assert entry.strength in (
            "diagnostic",
            "supportive",
            "ambiguous",
            "non_diagnostic",
        )


def test_catalog_rejects_malformed_entry(tmp_path):
    bad = {
        "catalog_version": "test",
        "feature_ids": ["adaptive_cruise_control"],
        "entries": [{"id": "broken"}],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="broken"):
        load_catalog(path)


def test_catalog_extensible_new_manufacturer(tmp_path, catalog):
    raw = json.loads(adas_evidence.CATALOG_PATH.read_text(encoding="utf-8"))
    raw["entries"].append(
        {
            "id": "toyota-camry-2023-dallas-test-only",
            "manufacturer": "Toyota",
            "models": ["Camry"],
            "model_years": [2023],
            "trim": None,
            "official_feature_name": "Test Feature (fixture only)",
            "feature": "adaptive_cruise_control",
            "control_category": "button",
            "control_description": "Synthetic test entry proving extension works",
            "symbol": "test triangle",
            "locations": ["steering_wheel"],
            "dedicated": True,
            "strength": "supportive",
            "match_keywords": ["test triangle camry"],
            "distinctions": "Fixture only; not a real equipment claim.",
            "cannot_infer": [],
            "sources": [
                {
                    "doc": "Test fixture",
                    "url": "",
                    "section": "",
                    "accessed": "2026-11-01",
                }
            ],
            "certainty": "provisional",
            "certainty_note": "Synthetic extension test.",
        }
    )
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    extended = load_catalog(path)
    assert len(extended.entries) == len(catalog.entries) + 1
    found = match_entry(extended, visible_object="test triangle camry button")
    assert found is not None
    assert found.id == "toyota-camry-2023-dallas-test-only"


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        ("Adaptive Cruise Control", "adaptive_cruise_control"),
        ("ACC with stop and go", "adaptive_cruise_control"),
        ("Smart Cruise Control", "adaptive_cruise_control"),
        ("Lane Departure Warning", "lane_departure_warning"),
        ("LDW", "lane_departure_warning"),
        ("Lane Keeping Assist", "lane_keeping_assist"),
        ("Lane Assist", "lane_keeping_assist"),
        ("Lane Centering Assist", "lane_centering_assist"),
        ("Lane Following Assist", "lane_centering_assist"),
        ("LFA", "lane_centering_assist"),
        ("Highway Driving Assist", "highway_assist"),
        ("Travel Assist", "travel_assist"),
        ("Hands-free driving", "hands_free_driving"),
        ("Cruise control", "cruise_control"),
        ("Heated seats", "unknown"),
        ("", "unknown"),
    ],
)
def test_normalize_feature_tiers(claim, expected):
    assert normalize_feature(claim) == expected


def test_match_dedicated_acc_button(catalog):
    entry = match_entry(
        catalog,
        visible_object="following-distance button",
        control_symbol="vehicle with distance bars",
        control_type="button",
    )
    assert entry is not None
    assert entry.id == "kia-forte-2021-scc-distance"
    assert entry.strength == "diagnostic"
    result = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="following-distance button",
        control_symbol="vehicle with distance bars",
        location="steering_wheel",
        dedicated=True,
    )
    assert result.supports_claim is True
    assert result.strength == "diagnostic"
    assert "lane_centering_assist" in result.cannot_infer
    assert "Kia Forte" in result.reference


def test_match_generic_cruise_not_acc(catalog):
    result = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="cruise set and resume buttons",
        location="steering_wheel",
    )
    assert result.entry_id == "generic-cruise-set-res"
    assert result.supports_claim is False
    assert result.strength == "non_diagnostic"
    cruise = interpret_control(
        catalog,
        "cruise_control",
        visible_object="cruise set and resume buttons",
        location="steering_wheel",
    )
    # Generic cruise is recognized for what it is (conventional cruise),
    # but the entry is non-diagnostic even there: buttons alone never
    # prove a system. What matters is it never supports ACC.
    assert cruise.supports_claim is False


def test_match_forte_lka_dash_button(catalog):
    result = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="lane keeping assist button",
        control_symbol="vehicle between lane lines",
        location="left_dashboard",
        dedicated=True,
        manufacturer_context="Kia Forte",
    )
    assert result.entry_id == "kia-forte-2019-2024-lka-button"
    assert result.supports_claim is True
    assert result.strength == "diagnostic"
    assert "lane_centering_assist" in result.cannot_infer
    assert "hands_free_driving" in result.cannot_infer


def test_match_stalk_menu_non_diagnostic(catalog):
    result = interpret_control(
        catalog,
        "lane_centering_assist",
        visible_object="driver assistance button",
        control_type="stalk button",
        location="stalk",
        dedicated=True,
        manufacturer_context="VW Jetta GLI",
    )
    assert result.entry_id == "vw-jetta-gli-assist-stalk"
    assert result.supports_claim is False
    assert "catalog wins" in result.location_note


def test_match_location_mismatch_downgrades(catalog):
    result = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="lane keeping assist button",
        control_symbol="vehicle between lane lines",
        location="steering_wheel",
        dedicated=True,
    )
    assert result.entry_id == "kia-forte-2019-2024-lka-button"
    assert result.strength == "supportive"
    assert "downgraded" in result.location_note
    # Downgraded diagnostic->supportive still supports the claim.
    assert result.supports_claim is True


def test_match_shared_control_downgrades(catalog):
    result = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="following-distance control",
        location="steering_wheel",
        dedicated=False,
    )
    assert result.strength == "supportive"
    assert result.supports_claim is True


def test_match_other_manufacturer_downgrades(catalog):
    result = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="lane keeping assist button",
        control_symbol="vehicle between lane lines",
        location="left_dashboard",
        dedicated=True,
        manufacturer_context="Volkswagen Golf",
    )
    assert result.entry_id == "kia-forte-2019-2024-lka-button"
    assert result.strength == "supportive"
    assert "different vehicle" in result.location_note


def test_match_unknown_control(catalog):
    assert (
        match_entry(catalog, visible_object="cup holder", control_type="trim") is None
    )
    result = interpret_control(
        catalog, "adaptive_cruise_control", visible_object="cup holder"
    )
    assert result.supports_claim is False
    assert result.strength == "unknown"
    assert result.entry_id is None


def test_match_skips_naming_entries(catalog):
    # "lane assist" wording alone matches no physical control: the VW
    # naming entry documents claim language, not a localizable object.
    assert match_entry(catalog, visible_object="lane assist decal on window") is None


def test_match_tie_is_unknown(tmp_path):
    raw = {
        "catalog_version": "test",
        "feature_ids": ["adaptive_cruise_control"],
        "entries": [
            {
                "id": f"tie-{i}",
                "manufacturer": None,
                "models": [],
                "model_years": [],
                "official_feature_name": "Tie",
                "feature": "adaptive_cruise_control",
                "control_category": "button",
                "control_description": "tie",
                "symbol": "tie",
                "locations": [],
                "dedicated": True,
                "strength": "supportive",
                "match_keywords": ["mystery widget"],
                "distinctions": "",
                "cannot_infer": [],
                "sources": [{"doc": "t", "url": "", "section": "", "accessed": ""}],
                "certainty": "definitional",
                "certainty_note": "",
            }
            for i in range(2)
        ],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    tied = load_catalog(path)
    assert match_entry(tied, visible_object="mystery widget") is None


def test_interpret_feature_mismatch_no_promotion(catalog):
    lka_for_centering = interpret_control(
        catalog,
        "lane_centering_assist",
        visible_object="lane keeping assist button",
        control_symbol="vehicle between lane lines",
        location="left_dashboard",
        dedicated=True,
    )
    assert lka_for_centering.supports_claim is False
    assert "not to the claimed lane_centering_assist" in (lka_for_centering.explanation)
    acc_for_steering = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
    )
    assert acc_for_steering.supports_claim is False


def test_steering_wheel_lanes_icon_is_not_auto_classified(catalog):
    # A steering-wheel-between-lanes icon is manufacturer-dependent: with
    # no identifying control text it matches nothing and stays unknown
    # rather than auto-becoming LKA or centering.
    result = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="steering wheel between lane markings icon",
        location="instrument_cluster",
    )
    assert result.supports_claim is False
    assert result.entry_id is None
