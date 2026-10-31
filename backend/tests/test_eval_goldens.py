"""Reconcile golden cases stay green (CI pin; zero inference)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from audits.models import Observation
from audits.pipeline import reconcile_claim

GOLDENS = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "evals"
        / "cases"
        / "v1"
        / "reconcile_goldens.json"
    ).read_text(encoding="utf-8")
)


def test_reconcile_goldens():
    photo_ids = [uuid.uuid4() for _ in range(4)]
    failures = []
    for case in GOLDENS["cases"]:
        rows = [
            Observation(
                source=item["source"],
                verdict=item["verdict"],
                photo_id=photo_ids[item["photo"]],
                extra=item.get("extra", {}),
            )
            for item in case["observations"]
        ]
        state, _ = reconcile_claim(rows)
        if state != case["expected"]:
            failures.append(f"{case['id']}: expected {case['expected']}, got {state}")
    assert failures == []
    assert len(GOLDENS["cases"]) >= 10
