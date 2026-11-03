"""Reconcile golden cases stay green (CI pin; zero inference)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from audits import adas_evidence
from audits.models import Observation
from audits.pipeline import reconcile_claim

GOLDEN_VERSIONS = ("v2", "v4", "v5")

# Golden sets encode the catalog they were written against: v2/v4 lock
# the manufacturer-specific narrowing (catalog v1); v5 exercises the
# cross-brand general patterns (catalog v2). Never repoint a set at a
# different catalog version without rewriting its expectations.
GOLDEN_CATALOGS = {
    "v2": "adas_catalog_v1.json",
    "v4": "adas_catalog_v1.json",
    "v5": "adas_catalog_v2.json",
}


def _load(version):
    return json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "evals"
            / "cases"
            / version
            / "reconcile_goldens.json"
        ).read_text(encoding="utf-8")
    )


def _catalog_for(version):
    return adas_evidence.load_catalog(
        Path(adas_evidence.__file__).resolve().parent / GOLDEN_CATALOGS[version]
    )


def test_reconcile_goldens():
    photo_ids = [uuid.uuid4() for _ in range(4)]
    failures = []
    for version in GOLDEN_VERSIONS:
        goldens = _load(version)
        catalog = _catalog_for(version)
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
            state, _ = reconcile_claim(
                rows, case.get("claim", ""), case.get("vehicle"), catalog=catalog
            )
            if state != case["expected"]:
                failures.append(
                    f"{version}/{case['id']}: expected {case['expected']}, got {state}"
                )
        assert len(goldens["cases"]) >= 10, version
    assert failures == []
