"""Bounded live smoke test: 3 Muse + 3 SAM calls on synthetic fixtures.

Approved by James Lane (PR #2 cost plan). Runs ONLY when
``VCC_LIVE_SMOKE=1`` and both provider keys are present; otherwise every
test skips. Never runs in CI. A hard $0.50 ledger guard fails the run if
spending ever exceeds the approved bound.

Doubles as the model-slug check: the first Muse call proves whether
``meta-models/muse-spark-open-1026`` resolves on Baseten. A model 404
fails loudly with ADR guidance instead of silently substituting a slug.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from PIL import Image

from providers import (
    BASETEN_MODEL_ID,
    SAM31_MODEL_ID,
    BasetenMuseAdapter,
    ImageAttachment,
    MetaSamAdapter,
    ProviderError,
    UsageLedger,
)
from providers.baseten import MuseRequest
from providers.meta import SamRequest

FIXTURES = (
    Path(__file__).resolve().parents[2] / "fixtures" / "synthetic-listings" / "photos"
)
SMOKE_SPEND_CAP_USD = 0.50

KEYS_PRESENT = bool(os.environ.get("BASETEN_API_KEY")) and bool(
    os.environ.get("META_API_KEY")
)
LIVE = os.environ.get("VCC_LIVE_SMOKE") == "1" and KEYS_PRESENT

needs_live = pytest.mark.skipif(
    not LIVE, reason="live smoke test (needs VCC_LIVE_SMOKE=1 + keys)"
)

# One ledger for the whole file so the final guard totals all 6 calls.
SMOKE_LEDGER = UsageLedger()
PER_CALL_CAP_USD = 0.10


def check_call_cost(label: str) -> None:
    record = SMOKE_LEDGER.records[-1]
    assert record.status == "ok", f"{label} failed: {record.error}"
    assert record.estimated_cost_usd is not None, f"{label} has unknown cost"
    assert record.estimated_cost_usd <= PER_CALL_CAP_USD, f"{label} over per-call cap"
    print(f"\n{label}: ${record.estimated_cost_usd:.4f}")


SYSTEM = (
    "You observe vehicle photographs for an ADAS listing audit. "
    "Report only what is visible. Never guess about functionality. "
    "Respond with exactly the requested JSON object and nothing else."
)


def observation_prompt(photo_id: str) -> str:
    # The server does not enforce a schema, so the exact shape is spelled
    # out in the prompt; the strict validator remains the real contract.
    return (
        "Advertised claims: adaptive cruise control. Inspect the photograph "
        "and respond with exactly this JSON shape and no other top-level keys: "
        '{"observations": [{"photo_id": "<id>", "claim_concept": "<normalized '
        'concept or \'none\'>", "visible_object": "<what is visible>", '
        '"description": "<what is actually visible>", "supports_localization": '
        '<true|false>, "ambiguous": <true|false>, "needs_crop": <true|false>, '
        '"needs_other_view": <true|false>, "notes": "<optional>"}]} '
        f'Use "observations": [] if nothing relevant is visible. photo_id '
        f"must be exactly {photo_id!r}."
    )


def load_fixture(name: str, mime: str) -> tuple[ImageAttachment, int, int]:
    data = (FIXTURES / name).read_bytes()
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    return ImageAttachment(photo_id=name, mime_type=mime, data=data), width, height


def muse_call(request: MuseRequest):
    adapter = BasetenMuseAdapter(ledger=SMOKE_LEDGER)
    try:
        return adapter.analyze(request)
    except ProviderError as exc:
        if getattr(exc, "status", None) == 404:
            pytest.fail(
                f"Baseten 404 for {BASETEN_MODEL_ID}: file the slug ADR and "
                "switch to the documented model id (same model, not a "
                f"substitution). Provider said: {exc}"
            )
        raise


@needs_live
def test_smoke_muse_observes_interior():
    image, _, _ = load_fixture("interior.png", "image/png")
    result = muse_call(
        MuseRequest(
            prompt_version="smoke-v1",
            system_instructions=SYSTEM,
            user_text=observation_prompt(image.photo_id),
            photo_ids=(image.photo_id,),
            images=(image,),
        ),
    )
    assert result.model == BASETEN_MODEL_ID
    assert isinstance(result.observations, tuple)
    print(f"\nMuse interior: {len(result.observations)} observations")
    check_call_cost("Muse interior")


@needs_live
def test_smoke_muse_observes_grille():
    image, _, _ = load_fixture("front.jpg", "image/jpeg")
    result = muse_call(
        MuseRequest(
            prompt_version="smoke-v1",
            system_instructions=SYSTEM,
            user_text=observation_prompt(image.photo_id),
            photo_ids=(image.photo_id,),
            images=(image,),
        ),
    )
    assert isinstance(result.observations, tuple)
    print(f"\nMuse grille: {len(result.observations)} observations")
    check_call_cost("Muse grille")


@needs_live
def test_smoke_muse_crop_recheck():
    image, width, height = load_fixture("interior.png", "image/png")
    with Image.open(io.BytesIO(image.data)) as full:
        crop = full.crop((width // 4, height // 4, width * 3 // 4, height * 3 // 4))
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
    crop_attachment = ImageAttachment(
        photo_id="interior.png#crop", mime_type="image/png", data=buffer.getvalue()
    )
    result = muse_call(
        MuseRequest(
            prompt_version="smoke-v1",
            system_instructions=SYSTEM,
            user_text="Re-examine this crop. "
            + observation_prompt(crop_attachment.photo_id),
            photo_ids=(crop_attachment.photo_id,),
            images=(crop_attachment,),
        ),
    )
    assert isinstance(result.observations, tuple)
    print(f"\nMuse crop: {len(result.observations)} observations")
    check_call_cost("Muse crop")


@needs_live
def test_smoke_sam_concept_localizes():
    image, width, height = load_fixture("interior.png", "image/png")
    adapter = MetaSamAdapter(ledger=SMOKE_LEDGER)
    result = adapter.analyze(
        SamRequest(
            photo=image,
            image_width=width,
            image_height=height,
            concept="steering wheel",
        )
    )
    assert result.model == SAM31_MODEL_ID
    print(f"\nSAM concept: {len(result.records)} records")
    check_call_cost("SAM concept")


@needs_live
def test_smoke_sam_box_prompt():
    image, width, height = load_fixture("front.jpg", "image/jpeg")
    adapter = MetaSamAdapter(ledger=SMOKE_LEDGER)
    result = adapter.analyze(
        SamRequest(
            photo=image,
            image_width=width,
            image_height=height,
            boxes=((width // 4, height // 2, width * 3 // 4, height - 10),),
        )
    )
    assert isinstance(result.records, tuple)
    print(f"\nSAM box: {len(result.records)} records")
    check_call_cost("SAM box")


@needs_live
def test_smoke_sam_zero_match_is_clean():
    image, width, height = load_fixture("front.jpg", "image/jpeg")
    adapter = MetaSamAdapter(ledger=SMOKE_LEDGER)
    result = adapter.analyze(
        SamRequest(
            photo=image, image_width=width, image_height=height, concept="red bicycle"
        )
    )
    assert result.records == ()
    print("\nSAM zero-match: clean empty completion")
    check_call_cost("SAM zero-match")


@needs_live
def test_zz_smoke_spend_guard():
    # Runs last (file order): totals all 6 calls against the approved bound.
    assert len(SMOKE_LEDGER.records) == 6
    totals = SMOKE_LEDGER.cost_by_provider()
    total = sum(totals.values())
    print(f"\nSmoke spend by provider: {totals} (cap ${SMOKE_SPEND_CAP_USD:.2f})")
    assert total <= SMOKE_SPEND_CAP_USD
