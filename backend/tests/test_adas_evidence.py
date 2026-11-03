"""Layer A/D: feature normalization, catalog schema, control matching.

Pure unit tests (no database, no providers, no inference): the mapping
from observed controls to supported capabilities must be deterministic
and must never invent equipment knowledge.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from audits import adas_evidence
from audits.adas_evidence import (
    Catalog,
    CatalogEntry,
    entry_applicable,
    explain_claim,
    interpret_control,
    iter_control_items,
    load_catalog,
    match_entry,
    normalize_feature,
)


@pytest.fixture()
def catalog() -> Catalog:
    return load_catalog()


@pytest.fixture()
def catalog_v1() -> Catalog:
    # Pins the preserved v1 narrowing semantics: v4 goldens and the
    # pre-existing narrowing tests below run against catalog v1, while
    # new cross-brand behavior runs against the v2 default.
    return load_catalog(adas_evidence.CATALOG_V1_PATH)


def test_catalog_loads_with_provenance(catalog_v1):
    # v1 pin: locks the preserved manufacturer-specific narrowing.
    catalog = catalog_v1
    assert catalog.version == "2026-11-01/v1"
    assert len(catalog.entries) == 6
    for entry in catalog.entries:
        assert entry.sources, entry.id
        assert entry.scope in ("manufacturer_specific", "general"), entry.id
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
            "scope": "manufacturer_specific",
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


def test_match_dedicated_acc_button(catalog_v1):
    # v1 pin: locks the preserved manufacturer-specific narrowing.
    catalog = catalog_v1
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
        vehicle=FORTE_2021,
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
    # The general mapping resolves identically with or without vehicle
    # identity: it is explicitly documented as cross-manufacturer.
    camry = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="cruise set and resume buttons",
        location="steering_wheel",
        vehicle=CAMRY_2020,
    )
    assert camry.entry_id == "generic-cruise-set-res"
    assert camry.supports_claim is False


def test_match_forte_lka_dash_button(catalog):
    result = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="lane keeping assist button",
        control_symbol="vehicle between lane lines",
        location="left_dashboard",
        dedicated=True,
        manufacturer_context="Kia Forte",
        vehicle=FORTE_2021,
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
        vehicle=JETTA_2022,
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
        vehicle=FORTE_2021,
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
        vehicle=FORTE_2021,
    )
    assert result.strength == "supportive"
    assert result.supports_claim is True


def test_match_other_manufacturer_rejected(catalog_v1):
    # v1 pin: locks the preserved manufacturer-specific narrowing.
    catalog = catalog_v1
    # F1 correction: a manufacturer mismatch rejects the entry instead of
    # merely downgrading it — a weakened match could still support the
    # wrong vehicle's claim.
    result = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="lane keeping assist button",
        control_symbol="vehicle between lane lines",
        location="left_dashboard",
        dedicated=True,
        manufacturer_context="Volkswagen Golf",
        vehicle=GOLF_2022,
    )
    assert result.entry_id == "kia-forte-2019-2024-lka-button"
    assert result.supports_claim is False
    assert result.strength == "unknown"
    assert "not verified for" in result.explanation


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
                "scope": "general",
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
        vehicle=FORTE_2021,
    )
    assert lka_for_centering.supports_claim is False
    assert "not to the claimed lane_centering_assist" in (lka_for_centering.explanation)
    acc_for_steering = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
        vehicle=FORTE_2021,
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


# --- F1: vehicle-specific catalog applicability -----------------------

FORTE_2021 = {"year": 2021, "make": "Kia", "model": "Forte", "trim": "GT-Line"}
GOLF_2022 = {"year": 2022, "make": "Volkswagen", "model": "Golf", "trim": ""}
CAMRY_2020 = {"year": 2020, "make": "Toyota", "model": "Camry", "trim": "SE"}
JETTA_2022 = {"year": 2022, "make": "Volkswagen", "model": "Jetta", "trim": ""}


def test_applicable_kia_acc_supported(catalog):
    result = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="following-distance button",
        control_symbol="vehicle with distance bars",
        location="steering_wheel",
        dedicated=True,
        vehicle=FORTE_2021,
    )
    assert result.supports_claim is True
    assert result.strength == "diagnostic"
    assert result.entry_id == "kia-forte-2021-scc-distance"


def test_applicable_kia_lka_supported(catalog):
    result = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="lane keeping assist button",
        control_symbol="vehicle between lane lines",
        location="left_dashboard",
        dedicated=True,
        vehicle=FORTE_2021,
    )
    assert result.supports_claim is True
    assert result.strength == "diagnostic"


def test_vw_golf_cannot_borrow_kia_lka(catalog_v1):
    # v1 pin: locks the preserved manufacturer-specific narrowing.
    catalog = catalog_v1
    result = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="lane keeping assist button",
        control_symbol="vehicle between lane lines",
        location="left_dashboard",
        dedicated=True,
        vehicle=GOLF_2022,
    )
    assert result.entry_id == "kia-forte-2019-2024-lka-button"
    assert result.supports_claim is False
    assert result.strength == "unknown"
    assert "Kia Forte" in result.explanation
    assert "Volkswagen Golf" in result.explanation


def test_toyota_cannot_borrow_kia_acc(catalog_v1):
    # v1 pin: locks the preserved manufacturer-specific narrowing.
    catalog = catalog_v1
    result = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
        manufacturer_context="Toyota Camry",
        vehicle=CAMRY_2020,
    )
    assert result.supports_claim is False
    assert "not verified for" in result.explanation


def test_model_year_out_of_range_rejected(catalog_v1):
    # v1 pin: locks the preserved manufacturer-specific narrowing.
    catalog = catalog_v1
    kwargs = {
        "visible_object": "following-distance button",
        "location": "steering_wheel",
        "dedicated": True,
    }
    too_new = interpret_control(
        catalog,
        "adaptive_cruise_control",
        **kwargs,
        vehicle={**FORTE_2021, "year": 2024},
    )
    assert too_new.supports_claim is False
    assert "2021" in too_new.explanation and "2022" in too_new.explanation
    in_range = interpret_control(
        catalog,
        "adaptive_cruise_control",
        **kwargs,
        vehicle={**FORTE_2021, "year": 2022},
    )
    assert in_range.supports_claim is True


def test_missing_vehicle_rejects_specific_entry(catalog_v1):
    # v1 pin: locks the preserved manufacturer-specific narrowing.
    catalog = catalog_v1
    result = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
        manufacturer_context="Kia Forte",
        vehicle=None,
    )
    assert result.entry_id == "kia-forte-2021-scc-distance"
    assert result.supports_claim is False
    assert result.strength == "unknown"
    assert "identity" in result.explanation


def test_incomplete_identity_rejected(catalog_v1):
    # v1 pin: locks the preserved manufacturer-specific narrowing.
    catalog = catalog_v1
    kwargs = {
        "visible_object": "following-distance button",
        "location": "steering_wheel",
        "dedicated": True,
    }
    for vehicle in ({}, {"make": "Kia"}, {"make": "Kia", "model": "Forte"}):
        result = interpret_control(
            catalog, "adaptive_cruise_control", **kwargs, vehicle=vehicle
        )
        assert result.supports_claim is False, vehicle
    # Year unknown while make/model known is still too uncertain.
    assert (
        interpret_control(
            catalog,
            "adaptive_cruise_control",
            **kwargs,
            vehicle={"make": "Kia", "model": "Forte", "year": None},
        ).supports_claim
        is False
    )


def test_missing_vin_does_not_invalidate_metadata(catalog):
    assert "vin" not in FORTE_2021
    result = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
        vehicle=dict(FORTE_2021),
    )
    assert result.supports_claim is True


def test_context_conflict_cannot_override_listing(catalog):
    result = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
        manufacturer_context="Volkswagen Jetta dashboard",
        vehicle=FORTE_2021,
    )
    assert result.supports_claim is True
    assert result.strength == "diagnostic"
    assert "listing metadata governs" in result.location_note


def _general_catalog() -> Catalog:
    entry = CatalogEntry(
        id="test-general-follow-function",
        scope="general",
        manufacturer=None,
        models=(),
        model_years=(),
        trim=None,
        official_feature_name="Follow-distance function (test)",
        feature="adaptive_cruise_control",
        control_category="button",
        control_description="test-only general mapping",
        symbol="test",
        locations=("steering_wheel",),
        dedicated=True,
        strength="supportive",
        match_keywords=("zztestfollow",),
        distinctions="test only",
        cannot_infer=(),
        sources=({"doc": "test"},),
        certainty="definitional",
        certainty_note="test",
    )
    return Catalog(
        version="test", feature_ids=("adaptive_cruise_control",), entries=(entry,)
    )


def test_general_entry_applies_across_manufacturers():
    catalog = _general_catalog()
    for vehicle in (CAMRY_2020, FORTE_2021, None, {}):
        result = interpret_control(
            catalog,
            "adaptive_cruise_control",
            visible_object="zztestfollow button",
            location="steering_wheel",
            dedicated=True,
            vehicle=vehicle,
        )
        assert result.supports_claim is True, vehicle
        assert result.strength == "supportive"


def test_entry_applicable_unit(catalog):
    entry = catalog.by_id("kia-forte-2021-scc-distance")
    assert entry_applicable(entry, FORTE_2021) == (True, "applicable")
    eligible, reason = entry_applicable(entry, CAMRY_2020)
    assert eligible is False
    assert "Toyota Camry" in reason
    assert entry_applicable(entry, None)[0] is False
    general = catalog.by_id("generic-cruise-set-res")
    assert entry_applicable(general, None) == (True, "general mapping")


def test_model_name_containment(catalog):
    entry = catalog.by_id("kia-forte-2019-2024-lka-button")
    assert entry_applicable(entry, {**FORTE_2021, "model": "Forte5"})[0] is True
    assert entry_applicable(entry, {**FORTE_2021, "model": "K5"})[0] is False


def test_year_string_coerced_uncertain_rejected(catalog_v1):
    # v1 pin: locks the preserved manufacturer-specific narrowing.
    catalog = catalog_v1
    kwargs = {
        "visible_object": "following-distance button",
        "location": "steering_wheel",
        "dedicated": True,
    }
    coerced = interpret_control(
        catalog,
        "adaptive_cruise_control",
        **kwargs,
        vehicle={**FORTE_2021, "year": "2021"},
    )
    assert coerced.supports_claim is True
    vague = interpret_control(
        catalog,
        "adaptive_cruise_control",
        **kwargs,
        vehicle={**FORTE_2021, "year": "unknown"},
    )
    assert vague.supports_claim is False


def test_trim_gating_with_synthetic_entry():
    base = CatalogEntry(
        id="test-trim-gt",
        scope="manufacturer_specific",
        manufacturer="Kia",
        models=("Forte",),
        model_years=(2021,),
        trim="GT",
        official_feature_name="Test",
        feature="adaptive_cruise_control",
        control_category="button",
        control_description="test",
        symbol="test",
        locations=("steering_wheel",),
        dedicated=True,
        strength="supportive",
        match_keywords=("zztrimtest",),
        distinctions="test",
        cannot_infer=(),
        sources=({"doc": "test"},),
        certainty="definitional",
        certainty_note="test",
    )
    catalog = Catalog(
        version="test", feature_ids=("adaptive_cruise_control",), entries=(base,)
    )
    kwargs = {"visible_object": "zztrimtest control", "location": "steering_wheel"}
    assert (
        interpret_control(
            catalog,
            "adaptive_cruise_control",
            **kwargs,
            vehicle={**FORTE_2021, "trim": "GT"},
        ).supports_claim
        is True
    )
    assert (
        interpret_control(
            catalog,
            "adaptive_cruise_control",
            **kwargs,
            vehicle={**FORTE_2021, "trim": "LX"},
        ).supports_claim
        is False
    )
    assert (
        interpret_control(
            catalog,
            "adaptive_cruise_control",
            **kwargs,
            vehicle={**FORTE_2021, "trim": ""},
        ).supports_claim
        is False
    )


def _entry_dict(**overrides):
    entry = {
        "id": "test-entry",
        "scope": "manufacturer_specific",
        "manufacturer": "Kia",
        "models": ["Forte"],
        "model_years": [2021],
        "trim": None,
        "official_feature_name": "Test",
        "feature": "adaptive_cruise_control",
        "control_category": "button",
        "control_description": "test",
        "symbol": "test",
        "locations": ["steering_wheel"],
        "dedicated": True,
        "strength": "supportive",
        "match_keywords": ["testkw"],
        "distinctions": "test",
        "cannot_infer": [],
        "sources": [{"doc": "test"}],
        "certainty": "definitional",
        "certainty_note": "test",
    }
    entry.update(overrides)
    return {
        "catalog_version": "test",
        "feature_ids": ["adaptive_cruise_control"],
        "entries": [entry],
    }


def test_scope_validation(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_entry_dict()), encoding="utf-8")
    assert load_catalog(good).entries[0].scope == "manufacturer_specific"
    for bad_kwargs in (
        {"scope": "universal"},
        {"scope": "general", "manufacturer": "Kia"},
        {"scope": "general", "models": ["Forte"]},
        {"scope": "manufacturer_specific", "manufacturer": ""},
        {"scope": "manufacturer_specific", "models": []},
    ):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(_entry_dict(**bad_kwargs)), encoding="utf-8")
        with pytest.raises(ValueError, match="test-entry"):
            load_catalog(path)
    missing = _entry_dict()
    del missing["entries"][0]["scope"]
    path = tmp_path / "missing.json"
    path.write_text(json.dumps(missing), encoding="utf-8")
    with pytest.raises(ValueError, match="test-entry"):
        load_catalog(path)


# --- F2: per-control evidence items -----------------------------------


def test_control_items_top_level_only():
    items = iter_control_items(
        {
            "visible_object": "button",
            "location": "stalk",
            "dedicated": False,
            "manufacturer_context": "Kia",
        }
    )
    assert len(items) == 1
    top = items[0]
    assert top.index == 0
    assert top.visible_object == "button"
    assert top.location == "stalk"
    assert top.dedicated is False
    assert top.manufacturer_context == "Kia"


def test_control_items_nested_extraction_and_inheritance():
    items = iter_control_items(
        {
            "visible_object": "button cluster",
            "location": "steering_wheel",
            "dedicated": True,
            "manufacturer_context": "Kia Forte",
            "controls": [
                {
                    "visible_object": "following-distance button",
                    "control_symbol": "DISTANCE",
                    "location": "steering_wheel",
                },
                {"visible_object": "lane keeping button"},
            ],
        }
    )
    assert [item.index for item in items] == [0, 1, 2]
    assert items[1].control_symbol == "DISTANCE"
    # Nested items inherit only the photo-level manufacturer context:
    # dedication and location are never copied across controls.
    assert items[1].dedicated is None
    assert items[1].manufacturer_context == "Kia Forte"
    assert items[2].location == "unknown"
    assert items[2].dedicated is None


def test_control_items_dedup_restatement():
    items = iter_control_items(
        {
            "visible_object": "following-distance button",
            "location": "steering_wheel",
            "controls": [
                {
                    "visible_object": "Following-Distance Button",
                    "location": "steering_wheel",
                }
            ],
        }
    )
    assert len(items) == 1


def test_control_items_skips_empty_and_non_dict():
    items = iter_control_items(
        {
            "visible_object": "cluster",
            "controls": [
                {"visible_object": "  "},
                "nope",
                {"visible_object": "real button"},
            ],
        }
    )
    assert [item.visible_object for item in items] == ["cluster", "real button"]


def test_control_items_ignores_non_list():
    items = iter_control_items({"visible_object": "cluster", "controls": "nope"})
    assert len(items) == 1


def test_control_items_capped_at_five():
    items = iter_control_items(
        {
            "visible_object": "top",
            "controls": [{"visible_object": f"b{i}"} for i in range(6)],
        }
    )
    assert len(items) == 5


# --- F3: explanation matches the supporting finding --------------------

PRESENT = adas_evidence.Verdict.PRESENT


def _obs_row(photo_id=None, **extra):
    return SimpleNamespace(
        id=uuid.uuid4(),
        photo_id=photo_id or uuid.uuid4(),
        verdict=PRESENT,
        extra={"visible_object": "control", **extra},
    )


def test_explain_prefers_supporter_over_unrelated_diagnostic(catalog):
    lka_row = _obs_row(
        visible_object="lane keeping assist button",
        control_symbol="vehicle between lane lines",
        location="left_dashboard",
        dedicated=True,
    )
    acc_row = _obs_row(
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
    )
    # The unrelated diagnostic control comes first: strength-only
    # selection would present it as the ACC justification.
    result = explain_claim(
        catalog, [lka_row, acc_row], "adaptive cruise control", FORTE_2021
    )
    assert result["supports_claim"] is True
    assert result["entry_id"] == "kia-forte-2021-scc-distance"
    assert result["observation_id"] == str(acc_row.id)
    assert result["photo_id"] == str(acc_row.photo_id)
    assert result["control_index"] == 0
    assert len(result["interpretations"]) == 2


def test_explain_two_supporters_picks_stronger(catalog):
    shared = _obs_row(
        visible_object="following-distance control",
        location="steering_wheel",
        dedicated=False,
    )
    dedicated = _obs_row(
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
    )
    result = explain_claim(
        catalog, [shared, dedicated], "adaptive cruise control", FORTE_2021
    )
    assert result["supports_claim"] is True
    assert result["strength"] == "diagnostic"
    assert result["observation_id"] == str(dedicated.id)


def test_explain_supportive_and_conflicting(catalog):
    support = _obs_row(
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
    )
    conflict = _obs_row(
        visible_object="cruise set and resume buttons",
        location="steering_wheel",
    )
    result = explain_claim(
        catalog, [support, conflict], "adaptive cruise control", FORTE_2021
    )
    assert result["supports_claim"] is True
    assert result["entry_id"] == "kia-forte-2021-scc-distance"
    kept = result["interpretations"]
    assert [entry["supports_claim"] for entry in kept] == [True, False]
    assert kept[1]["entry_id"] == "generic-cruise-set-res"


def test_explain_no_supporters_states_limitation(catalog):
    row = _obs_row(
        visible_object="cruise set and resume buttons",
        location="steering_wheel",
    )
    result = explain_claim(catalog, [row], "adaptive cruise control", FORTE_2021)
    assert result["supports_claim"] is False
    assert result["entry_id"] == "generic-cruise-set-res"
    assert "not to the claimed adaptive_cruise_control" in result["explanation"]
    assert result["observation_id"] == str(row.id)
    assert result["photo_id"] == str(row.photo_id)


def test_explain_multi_photo_provenance(catalog):
    photo_a, photo_b = uuid.uuid4(), uuid.uuid4()
    weak = _obs_row(
        photo_id=photo_a,
        visible_object="cruise set and resume buttons",
        location="steering_wheel",
    )
    strong = _obs_row(
        photo_id=photo_b,
        visible_object="following-distance button",
        location="steering_wheel",
        dedicated=True,
    )
    result = explain_claim(
        catalog, [weak, strong], "adaptive cruise control", FORTE_2021
    )
    assert result["photo_id"] == str(photo_b)
    assert result["entry_id"] == "kia-forte-2021-scc-distance"
    assert {entry["photo_id"] for entry in result["interpretations"]} == {
        str(photo_a),
        str(photo_b),
    }


def test_explain_returns_none_without_keyed_rows(catalog):
    rows = [
        SimpleNamespace(
            id=uuid.uuid4(),
            photo_id=uuid.uuid4(),
            verdict=adas_evidence.Verdict.ABSENT,
            extra={"visible_object": "following-distance button"},
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            photo_id=uuid.uuid4(),
            verdict=PRESENT,
            extra={"notes": "no control identification"},
        ),
    ]
    assert explain_claim(catalog, rows, "adaptive cruise control", FORTE_2021) is None


# --- Task 1: keyword-collision ranking ----------------------------------


def test_collision_specific_entry_wins_for_valid_vehicle(catalog):
    # "smart cruise button" hits the Kia SCC phrase and the generic
    # "cruise" word: raw counts tie 1-1, but the documented phrase is
    # the more specific identifier and the vehicle is applicable.
    entry = match_entry(
        catalog, visible_object="smart cruise button", vehicle=FORTE_2021
    )
    assert entry is not None
    assert entry.id == "kia-forte-2021-scc-distance"
    result = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="smart cruise button",
        location="steering_wheel",
        dedicated=True,
        vehicle=FORTE_2021,
    )
    assert result.supports_claim is True
    assert result.strength == "diagnostic"


def test_collision_wrong_vehicle_still_rejected(catalog_v1):
    # v1 pin: locks the preserved manufacturer-specific narrowing.
    catalog = catalog_v1
    entry = match_entry(
        catalog, visible_object="smart cruise button", vehicle=CAMRY_2020
    )
    assert entry is not None
    assert entry.id == "kia-forte-2021-scc-distance"
    result = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="smart cruise button",
        location="steering_wheel",
        dedicated=True,
        vehicle=CAMRY_2020,
    )
    # Two-phase lookup: the inapplicable Forte entry falls through to the
    # applicable general cruise mapping, which still cannot support the
    # ACC claim (generic cruise never establishes ACC).
    assert result.entry_id == "generic-cruise-set-res"
    assert result.supports_claim is False
    assert "not to the claimed adaptive_cruise_control" in result.explanation


def test_applicability_breaks_specificity_tie(catalog_v1):
    # v1 pin: locks the preserved manufacturer-specific narrowing.
    catalog = catalog_v1
    # "scc lka button" matches one 3-letter identifier on each side:
    # identical specificity. For a 2023 Forte only the LKA entry is
    # applicable (the SCC entry covers 2021-2022), so it must win; for
    # a 2021 Forte both apply and the control stays genuinely unknown.
    forte_2023 = {**FORTE_2021, "year": 2023}
    winner = match_entry(catalog, visible_object="scc lka button", vehicle=forte_2023)
    assert winner is not None
    assert winner.id == "kia-forte-2019-2024-lka-button"
    assert (
        match_entry(catalog, visible_object="scc lka button", vehicle=FORTE_2021)
        is None
    )
    assert (
        interpret_control(
            catalog,
            "lane_keeping_assist",
            visible_object="scc lka button",
            vehicle=forte_2023,
        ).supports_claim
        is True
    )
    assert (
        interpret_control(
            catalog,
            "adaptive_cruise_control",
            visible_object="scc lka button",
            vehicle=forte_2023,
        ).supports_claim
        is False
    )


def test_genuine_tie_stays_unknown_with_vehicle(tmp_path):
    raw = {
        "catalog_version": "test",
        "feature_ids": ["adaptive_cruise_control"],
        "entries": [
            {
                "id": f"tie-{i}",
                "scope": "general",
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
    path = tmp_path / "tied.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    tied = load_catalog(path)
    assert (
        match_entry(tied, visible_object="mystery widget", vehicle=FORTE_2021) is None
    )


def test_cruise_wording_never_matches_acc_entry(catalog):
    entry = match_entry(
        catalog, visible_object="cruise control button", vehicle=FORTE_2021
    )
    assert entry is not None
    assert entry.id == "generic-cruise-set-res"
    result = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="cruise control button",
        location="steering_wheel",
        vehicle=FORTE_2021,
    )
    assert result.supports_claim is False


def test_claim_wording_alone_never_establishes_acc(catalog):
    # The model echoing the advertised feature ("adaptive cruise
    # control") with no physical control description must not resolve
    # to an ACC-specific entry.
    result = interpret_control(
        catalog,
        "adaptive_cruise_control",
        visible_object="adaptive cruise control",
        location="steering_wheel",
        dedicated=True,
        vehicle=FORTE_2021,
    )
    assert result.entry_id == "generic-cruise-set-res"
    assert result.supports_claim is False


# --- Cross-brand general patterns (v2) ---


def _pattern_catalog() -> Catalog:
    """One specific entry plus general-pattern entries for precedence tests.

    Self-contained (no v2 file dependency): the scope, ceiling, and
    fallback semantics under test, not the shipped v2 content.
    """
    raw = {
        "catalog_version": "test-patterns",
        "feature_ids": [
            "adaptive_cruise_control",
            "lane_keeping_assist",
            "lane_centering_assist",
        ],
        "entries": [
            {
                "id": "test-specific-lka",
                "scope": "manufacturer_specific",
                "manufacturer": "Kia",
                "models": ["Forte"],
                "model_years": [2021],
                "trim": None,
                "official_feature_name": "Test LKA",
                "feature": "lane_keeping_assist",
                "control_category": "button",
                "control_description": "test specific LKA button",
                "symbol": "test",
                "locations": ["left_dashboard"],
                "dedicated": True,
                "strength": "diagnostic",
                "match_keywords": ["zzlka"],
                "distinctions": "test",
                "cannot_infer": ["lane_centering_assist"],
                "sources": [{"doc": "t", "url": "", "section": "", "accessed": ""}],
                "certainty": "definitional",
                "certainty_note": "test",
            },
            {
                "id": "test-pattern-lka",
                "scope": "general_pattern",
                "manufacturer": None,
                "models": [],
                "model_years": [],
                "trim": None,
                "official_feature_name": "Test general LKA pattern",
                "feature": "lane_keeping_assist",
                "control_category": "button",
                "control_description": "test general LKA button",
                "symbol": "test",
                # Broad documented set (mirrors the real v2 entries): no
                # location in the property grid downgrades the pattern.
                "locations": [
                    "steering_wheel",
                    "left_dashboard",
                    "stalk",
                    "instrument_cluster",
                    "center_console",
                ],
                "dedicated": True,
                "strength": "supportive",
                "verdict_ceiling": "visually_supported",
                "match_keywords": ["zzlka"],
                "distinctions": "test",
                "cannot_infer": ["lane_centering_assist"],
                "sources": [{"doc": "t", "url": "", "section": "", "accessed": ""}],
                "certainty": "provisional",
                "certainty_note": "test",
            },
            {
                "id": "test-specific-menu",
                "scope": "manufacturer_specific",
                "manufacturer": "Kia",
                "models": ["Forte"],
                "model_years": [2021],
                "trim": None,
                "official_feature_name": "Test assistance menu",
                "feature": None,
                "control_category": "menu entry",
                "control_description": "test shared menu",
                "symbol": "test",
                "locations": ["infotainment"],
                "dedicated": False,
                "strength": "non_diagnostic",
                "match_keywords": ["zzmenu"],
                "distinctions": "test",
                "cannot_infer": ["lane_keeping_assist"],
                "sources": [{"doc": "t", "url": "", "section": "", "accessed": ""}],
                "certainty": "definitional",
                "certainty_note": "test",
            },
            {
                "id": "test-pattern-supportive-menu-text",
                "scope": "general_pattern",
                "manufacturer": None,
                "models": [],
                "model_years": [],
                "trim": None,
                "official_feature_name": "Test general pattern on menu text",
                "feature": "lane_keeping_assist",
                "control_category": "button",
                "control_description": "test",
                "symbol": "test",
                "locations": ["infotainment"],
                "dedicated": True,
                "strength": "supportive",
                "verdict_ceiling": "visually_supported",
                "match_keywords": ["zzmenu"],
                "distinctions": "test",
                "cannot_infer": [],
                "sources": [{"doc": "t", "url": "", "section": "", "accessed": ""}],
                "certainty": "provisional",
                "certainty_note": "test",
            },
        ],
    }
    import os
    import tempfile

    fd, name = tempfile.mkstemp(suffix=".json")
    path = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(raw, handle)
    return load_catalog(path)


def _pattern_entry(tmp_path, **overrides):
    base = {
        "id": "test-pattern-x",
        "scope": "general_pattern",
        "manufacturer": None,
        "models": [],
        "model_years": [],
        "trim": None,
        "official_feature_name": "Test pattern",
        "feature": "lane_keeping_assist",
        "control_category": "button",
        "control_description": "test",
        "symbol": "test",
        "locations": ["steering_wheel"],
        "dedicated": True,
        "strength": "supportive",
        "verdict_ceiling": "visually_supported",
        "match_keywords": ["zzpattern"],
        "distinctions": "test",
        "cannot_infer": [],
        "sources": [{"doc": "t", "url": "", "section": "", "accessed": ""}],
        "certainty": "provisional",
        "certainty_note": "test",
    }
    base.update(overrides)
    raw = {
        "catalog_version": "test",
        "feature_ids": ["lane_keeping_assist"],
        "entries": [base],
    }
    path = tmp_path / "pattern.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_general_pattern_entry_loads_with_ceiling():
    catalog = _pattern_catalog()
    entry = catalog.by_id("test-pattern-lka")
    assert entry is not None
    assert entry.scope == "general_pattern"
    assert entry.verdict_ceiling == "visually_supported"


def test_general_pattern_rejects_higher_ceiling(tmp_path):
    with pytest.raises(ValueError, match="ceiling"):
        load_catalog(_pattern_entry(tmp_path, verdict_ceiling="verified"))


def test_general_pattern_rejects_diagnostic_strength(tmp_path):
    with pytest.raises(ValueError, match="strength"):
        load_catalog(_pattern_entry(tmp_path, strength="diagnostic"))


def test_general_pattern_rejects_missing_ceiling(tmp_path):
    raw = json.loads((_pattern_entry(tmp_path)).read_text(encoding="utf-8"))
    del raw["entries"][0]["verdict_ceiling"]
    path = tmp_path / "no-ceiling.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="ceiling"):
        load_catalog(path)


def test_general_pattern_scope_always_applicable():
    catalog = _pattern_catalog()
    entry = catalog.by_id("test-pattern-lka")
    for vehicle in (FORTE_2021, CAMRY_2020, GOLF_2022, {}, None):
        eligible, _ = adas_evidence.entry_applicable(entry, vehicle)
        assert eligible is True


def test_general_pattern_verdict_bounds_property():
    # Every general-pattern interpretation across vehicles x locations:
    # never diagnostic, and support only ever at supportive strength
    # (the verdict layer caps photo-only evidence at visually_supported;
    # verified/contradicted need authoritative vehicle-specific evidence).
    catalog = _pattern_catalog()
    vehicles = [FORTE_2021, CAMRY_2020, GOLF_2022, {}, None, {"make": "Kia"}]
    locations = [
        "steering_wheel",
        "left_dashboard",
        "stalk",
        "instrument_cluster",
        "center_console",
        "unknown",
        "exotic_location",
    ]
    for vehicle in vehicles:
        for location in locations:
            result = interpret_control(
                catalog,
                "lane_keeping_assist",
                visible_object="zzlka button",
                location=location,
                dedicated=True,
                vehicle=vehicle,
            )
            if vehicle == FORTE_2021:
                # Applicable specific entry wins over the general pattern.
                assert result.entry_id == "test-specific-lka"
                if location in ("left_dashboard", "unknown", "exotic_location"):
                    assert result.strength == "diagnostic", (vehicle, location)
                else:
                    # Undocumented placement downgrades one level (rule 3).
                    assert result.strength == "supportive", (vehicle, location)
            else:
                assert result.entry_id == "test-pattern-lka"
                assert result.strength == "supportive", (vehicle, location)
                assert "general" in result.explanation.lower()
            assert result.supports_claim is True


def test_general_pattern_code_cap_overrides_declared_strength():
    # Structural enforcement below the loader: even a hand-built
    # general-pattern entry declaring diagnostic strength is capped at
    # supportive by interpret_control, so photo evidence built on it
    # can never exceed visually_supported however it was constructed.
    raw_pattern = {
        "id": "test-pattern-hot",
        "scope": "general_pattern",
        "manufacturer": None,
        "models": [],
        "model_years": [],
        "trim": None,
        "official_feature_name": "Test hot pattern",
        "feature": "lane_keeping_assist",
        "control_category": "button",
        "control_description": "test",
        "symbol": "test",
        "locations": [
            "steering_wheel",
            "left_dashboard",
            "stalk",
            "instrument_cluster",
            "center_console",
        ],
        "dedicated": True,
        "strength": "diagnostic",
        "verdict_ceiling": "visually_supported",
        "match_keywords": ["zzhot"],
        "distinctions": "test",
        "cannot_infer": [],
        "sources": [{"doc": "t", "url": "", "section": "", "accessed": ""}],
        "certainty": "provisional",
        "certainty_note": "test",
    }
    hot = adas_evidence.CatalogEntry(
        id=raw_pattern["id"],
        scope=raw_pattern["scope"],
        manufacturer=raw_pattern["manufacturer"],
        models=tuple(raw_pattern["models"]),
        model_years=tuple(raw_pattern["model_years"]),
        trim=raw_pattern["trim"],
        official_feature_name=raw_pattern["official_feature_name"],
        feature=raw_pattern["feature"],
        control_category=raw_pattern["control_category"],
        control_description=raw_pattern["control_description"],
        symbol=raw_pattern["symbol"],
        locations=tuple(raw_pattern["locations"]),
        dedicated=bool(raw_pattern["dedicated"]),
        strength=raw_pattern["strength"],
        match_keywords=tuple(raw_pattern["match_keywords"]),
        distinctions=raw_pattern["distinctions"],
        cannot_infer=tuple(raw_pattern["cannot_infer"]),
        sources=tuple(raw_pattern["sources"]),
        certainty=raw_pattern["certainty"],
        certainty_note=raw_pattern["certainty_note"],
        verdict_ceiling=raw_pattern["verdict_ceiling"],
    )
    catalog = adas_evidence.Catalog(
        version="test-hot", feature_ids=("lane_keeping_assist",), entries=(hot,)
    )
    result = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="zzhot button",
        location="steering_wheel",
        dedicated=True,
        vehicle=GOLF_2022,
    )
    assert result.entry_id == "test-pattern-hot"
    assert result.strength == "supportive"
    assert result.supports_claim is True
    assert "reviewer should confirm" in result.explanation


def test_specific_entry_wins_over_general_pattern():
    catalog = _pattern_catalog()
    result = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="zzlka button",
        location="left_dashboard",
        dedicated=True,
        vehicle=FORTE_2021,
    )
    assert result.entry_id == "test-specific-lka"
    assert result.strength == "diagnostic"
    assert result.supports_claim is True


def test_inapplicable_specific_falls_through_to_general():
    catalog = _pattern_catalog()
    result = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="zzlka button",
        location="steering_wheel",
        dedicated=True,
        vehicle=CAMRY_2020,
    )
    assert result.entry_id == "test-pattern-lka"
    assert result.strength == "supportive"
    assert result.supports_claim is True


def test_documented_menu_never_upgraded_by_general():
    catalog = _pattern_catalog()
    result = interpret_control(
        catalog,
        "lane_keeping_assist",
        visible_object="zzmenu control",
        location="infotainment",
        dedicated=False,
        vehicle=FORTE_2021,
    )
    assert result.entry_id == "test-specific-menu"
    assert result.supports_claim is False


def test_default_catalog_is_v2_with_general_patterns():
    # A.4: production default is catalog v2 (v1 stays available by path
    # for the pinned v2/v4 goldens). Switching the default back to v1
    # must fail this test.
    catalog = load_catalog()
    assert catalog.version == "2026-11-03/v2"
    patterns = [entry for entry in catalog.entries if entry.scope == "general_pattern"]
    assert len(patterns) >= 4
    assert all(entry.verdict_ceiling == "visually_supported" for entry in patterns)


def test_v2_preserves_v1_entries_byte_semantics():
    v1 = json.loads(adas_evidence.CATALOG_V1_PATH.read_text(encoding="utf-8"))
    v2path = adas_evidence.CATALOG_PATH.parent / "adas_catalog_v2.json"
    v2 = json.loads(v2path.read_text(encoding="utf-8"))
    assert v2["feature_ids"] == v1["feature_ids"]
    assert v2["entries"][: len(v1["entries"])] == v1["entries"]
    assert len(v2["entries"]) > len(v1["entries"])
