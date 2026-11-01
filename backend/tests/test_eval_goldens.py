"""Reconcile golden cases stay green (CI pin; zero inference)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from audits.models import Observation
from audits.pipeline import reconcile_claim

GOLDEN_VERSIONS = ("v2", "v3")


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


def test_reconcile_goldens():
    photo_ids = [uuid.uuid4() for _ in range(4)]
    failures = []
    for version in GOLDEN_VERSIONS:
        goldens = _load(version)
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
            state, _ = reconcile_claim(rows, case.get("claim", ""))
            if state != case["expected"]:
                failures.append(
                    f"{version}/{case['id']}: expected {case['expected']}, got {state}"
                )
        assert len(goldens["cases"]) >= 10, version
    assert failures == []
