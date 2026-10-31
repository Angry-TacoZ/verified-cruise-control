"""PR #2 eval runner: reconcile goldens (free) and live Muse quality (gated).

Usage:
    uv run python evals/run.py reconcile            # zero inference, always safe
    uv run python evals/run.py muse-live            # 1 live call, needs approval

``muse-live`` refuses to run unless ``VCC_LIVE_SMOKE=1`` is set, a Baseten
key is present, and the estimated spend is under ``--max-spend`` (default
$0.25). Never run it in CI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from audits.models import Observation
from audits.pipeline import (
    PROMPT_VERSION,
    SYSTEM_INSTRUCTIONS,
    build_user_text,
    reconcile_claim,
)

CASES = REPO / "evals" / "cases" / "v1"
RESULTS = REPO / "evals" / "results"
FIXTURES = REPO / "fixtures" / "synthetic-listings" / "photos"
MIME = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def run_reconcile() -> dict:
    goldens = json.loads((CASES / "reconcile_goldens.json").read_text())
    photo_ids = [uuid.uuid4() for _ in range(4)]
    scored = []
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
        state, summary = reconcile_claim(rows)
        scored.append(
            {
                "id": case["id"],
                "expected": case["expected"],
                "got": state,
                "summary": summary,
                "pass": state == case["expected"],
            }
        )
    passed = sum(1 for item in scored if item["pass"])
    return {
        "eval": "reconcile",
        "rule": goldens["rule"],
        "cases_version": goldens["version"],
        "passed": passed,
        "total": len(scored),
        "cases": scored,
    }


def run_muse_live(max_spend: float) -> dict:
    from providers.base import ImageAttachment
    from providers.baseten import BasetenMuseAdapter, MuseRequest

    if os.environ.get("VCC_LIVE_SMOKE") != "1":
        raise SystemExit("refusing: set VCC_LIVE_SMOKE=1 to run live evals")
    truth = json.loads((CASES / "muse_fixture_ground_truth.json").read_text())
    claims = truth["claims"]
    acceptable = set(truth["acceptable_verdicts"])
    attachments = []
    for name in truth["photos"]:
        data = (FIXTURES / name).read_bytes()
        attachments.append(
            ImageAttachment(photo_id=name, mime_type=MIME[Path(name).suffix], data=data)
        )
    adapter = BasetenMuseAdapter()
    adapter.require_configured()
    request = MuseRequest(
        prompt_version=PROMPT_VERSION,
        system_instructions=SYSTEM_INSTRUCTIONS,
        user_text=build_user_text(claims, truth["photos"]),
        photo_ids=tuple(truth["photos"]),
        images=tuple(attachments),
        # Six (claim, photo) observations with descriptions do not fit the
        # 2048-token default; the first live attempt truncated and was
        # correctly quarantined as malformed.
        max_output_tokens=4096,
    )
    result = adapter.analyze(request)
    usage = result.usage
    cost = usage.estimated_cost_usd
    if cost is None or cost > max_spend:
        raise SystemExit(f"refusing: live cost {cost} exceeds bound ${max_spend}")
    scored = []
    for item in result.observations:
        if item["ambiguous"]:
            verdict = "unclear"
        elif str(item["visible_object"]).strip().lower() == "none":
            verdict = "absent"
        else:
            verdict = "present"
        scored.append(
            {
                "photo_id": item["photo_id"],
                "claim": item["claim_concept"],
                "verdict": verdict,
                "pass": verdict in acceptable,
                "description": item["description"],
            }
        )
    present = sum(1 for item in scored if item["verdict"] == "present")
    return {
        "eval": "muse-live",
        "prompt_version": PROMPT_VERSION,
        "model": result.model,
        "cases_version": truth["version"],
        "unsupported_inference_rate": present / len(scored) if scored else 1.0,
        "passed": sum(1 for item in scored if item["pass"]),
        "total": len(scored),
        "cost_usd": cost,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "latency_ms": usage.latency_ms,
            "pricing_version": usage.pricing_version,
        },
        "cases": scored,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run PR #2 evals")
    parser.add_argument("eval", choices=["reconcile", "muse-live"])
    parser.add_argument("--max-spend", type=float, default=0.25)
    args = parser.parse_args()
    if args.eval == "reconcile":
        report = run_reconcile()
        name = f"{date.today().isoformat()}-reconcile-v1.json"
    else:
        report = run_muse_live(args.max_spend)
        name = f"{date.today().isoformat()}-muse-{PROMPT_VERSION}.json"
    dest = RESULTS / name
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{report['passed']}/{report['total']} passed -> {dest}")
    if report["passed"] != report["total"]:
        raise SystemExit("eval failures recorded")


if __name__ == "__main__":
    main()
