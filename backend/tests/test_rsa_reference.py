"""RSA general ADAS knowledge reference (PR #3 Milestone 1).

Pure unit tests (no database, no providers): the structured reference
must stay source-cited, generic-only, and terminology-precise, and the
guidance accessor must stay short and presence-neutral.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audits import rsa_reference
from audits.rsa_reference import (
    get_reference,
    guidance_for,
    load_reference,
)

REFERENCE_JSON = (
    Path(__file__).resolve().parents[2] / "backend" / "audits" / "rsa_reference.json"
)


@pytest.fixture()
def reference():
    return load_reference()


@pytest.fixture()
def raw():
    return json.loads(REFERENCE_JSON.read_text(encoding="utf-8"))


def test_reference_loads_with_version(reference):
    assert reference.version == rsa_reference.REFERENCE_VERSION == "2026-09-rsa-v1"
    assert len(reference.entries) >= 15
    assert "Road Safety Authority" in reference.source_document
    assert reference.market_scope != ""
    assert get_reference() is get_reference()


def test_schema_complete_with_page_provenance(reference):
    for entry in reference.entries:
        assert entry.id and entry.names and entry.definition
        assert entry.assistance_mode in ("warn", "intervene", "continuous", "inform")
        assert entry.sources, entry.id
        for source in entry.sources:
            assert source["section"] and 3 <= source["page"] <= 21, entry.id
        for symbol in entry.symbols:
            assert symbol.description and symbol.location_hint, entry.id
            assert 3 <= symbol.page <= 21, entry.id
            assert symbol.limitation, entry.id
        assert entry.visual_support, entry.id
        assert entry.cannot_prove, entry.id
        assert entry.limitations, entry.id


def test_all_entries_generic_only(raw):
    for item in raw["entries"]:
        assert item["scope"] == "general", item["id"]
        for maker_key in ("manufacturer", "models", "model_years", "trim"):
            assert maker_key not in item, (item["id"], maker_key)


def test_no_universal_icon_claims(raw):
    text = json.dumps(raw).lower()
    assert "standardised" not in text
    assert "universal" not in text
    assert "always means" not in text


def test_essential_distinctions_preserved(reference):
    ids = {entry.id for entry in reference.entries}
    for required in (
        "cruise_control",
        "adaptive_cruise_control",
        "acc_stop_and_go",
        "lane_departure_warning",
        "lane_keeping_assist",
        "lane_centering_assist",
        "forward_collision_warning",
        "automatic_emergency_braking",
        "reversing_camera",
        "parking_sensors",
        "semi_automatic_parking",
        "fully_automatic_parking",
    ):
        assert required in ids, required
    by_id = reference.by_id
    assert "lane_centering_assist" in by_id("lane_keeping_assist").confusable_with
    assert "lane_keeping_assist" in by_id("lane_centering_assist").confusable_with
    assert "cruise_control" in by_id("adaptive_cruise_control").confusable_with
    assert (
        "automatic_emergency_braking"
        in by_id("forward_collision_warning").confusable_with
    )
    # A camera display is never framed as parking automation.
    assert "automated parking" in by_id("reversing_camera").cannot_prove
    # A following-distance control does not establish Stop & Go.
    assert "Stop & Go" in by_id("adaptive_cruise_control").cannot_prove
    assert "stopping/resumption" in by_id("acc_stop_and_go").cannot_prove


def test_guidance_is_short_and_presence_neutral():
    snippet = guidance_for("adaptive_cruise_control")
    assert snippet != ""
    assert len(snippet) <= 600
    assert "distance" in snippet
    assert "cruise" in snippet
    assert "Report only visible evidence" in snippet
    assert "is equipped" not in snippet
    assert "vehicle has" not in snippet


def test_guidance_covers_normalized_features():
    for feature in (
        "cruise_control",
        "adaptive_cruise_control",
        "lane_keeping_assist",
        "lane_departure_warning",
        "lane_centering_assist",
    ):
        assert guidance_for(feature) != "", feature
    # No forced mapping for capabilities the guide does not identify.
    assert guidance_for("hands_free_driving") == ""
    assert guidance_for("unknown") == ""
    assert guidance_for("not_a_feature") == ""


def test_guidance_length_cap():
    snippet = guidance_for("adaptive_cruise_control", max_chars=60)
    assert len(snippet) <= 60
    assert snippet.endswith("…")


def test_loader_rejects_non_generic_entry(tmp_path):
    base = {
        "reference_version": "test",
        "source_document": "test",
        "market_scope": "test",
        "entries": [
            {
                "id": "bad",
                "names": ["Bad"],
                "definition": "bad",
                "assistance_mode": "warn",
                "symbols": [],
                "indicator_location": "test",
                "confusable_with": [],
                "visual_support": "test",
                "cannot_prove": "test",
                "sources": [{"section": "1", "page": 3}],
                "scope": "manufacturer_specific",
                "limitations": "test",
            }
        ],
    }
    path = tmp_path / "rsa.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(ValueError, match="bad"):
        load_reference(path)


def test_loader_rejects_missing_provenance(tmp_path):
    base = {
        "reference_version": "test",
        "source_document": "test",
        "market_scope": "test",
        "entries": [
            {
                "id": "noproof",
                "names": ["NoProof"],
                "definition": "test",
                "assistance_mode": "warn",
                "symbols": [],
                "indicator_location": "test",
                "confusable_with": [],
                "visual_support": "test",
                "cannot_prove": "test",
                "sources": [],
                "scope": "general",
                "limitations": "test",
            }
        ],
    }
    path = tmp_path / "rsa.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(ValueError, match="noproof"):
        load_reference(path)
