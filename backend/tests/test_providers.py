"""Provider contracts: identifiers, config, payloads, strict validators.

No test here touches the network: live paths run against httpx
MockTransport. The 6-call live smoke test is separate (test_live_smoke.py,
explicitly gated, never in CI).
"""

from __future__ import annotations

import json

import httpx
import pytest

from providers import (
    BASETEN_API_BASE,
    BASETEN_MODEL_ID,
    META_API_BASE,
    SAM31_MODEL_ID,
    BasetenMuseAdapter,
    ImageAttachment,
    MetaSamAdapter,
    ProviderAuthError,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimitError,
    ProviderResponseError,
    UsageLedger,
    UsageRecord,
)
from providers.baseten import (
    BASETEN_PRICING_VERSION,
    MuseObservationResult,
    MuseRequest,
    validate_observation_payload,
)
from providers.meta import (
    META_PRICE_PER_IMAGE_USD,
    META_PRICING_VERSION,
    SamRequest,
    parse_segmentation_records,
    parse_sse_output_text,
)

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x04\x00\x00\x00\x04"
    b"\x08\x02\x00\x00\x00\x00\x00\x00\x00IDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\x0d\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def attachment(photo_id: str = "p1") -> ImageAttachment:
    return ImageAttachment(photo_id=photo_id, mime_type="image/png", data=TINY_PNG)


def muse_request(**overrides) -> MuseRequest:
    base = {
        "prompt_version": "v1",
        "system_instructions": "s",
        "user_text": "u",
        "photo_ids": ("p1",),
        "images": (attachment("p1"),),
    }
    base.update(overrides)
    return MuseRequest(**base)


def sam_request(**overrides) -> SamRequest:
    base = {
        "photo": attachment("p1"),
        "image_width": 160,
        "image_height": 90,
        "concept": "steering wheel button",
    }
    base.update(overrides)
    return SamRequest(**base)


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_exact_model_identifiers():
    assert BASETEN_MODEL_ID == "meta-models/muse-spark-open-1026"
    assert BASETEN_API_BASE == "https://inference.baseten.co/v1"
    assert SAM31_MODEL_ID == "sam-3.1"
    assert META_API_BASE == "https://api.meta.ai/v1"
    assert BasetenMuseAdapter.model_id == BASETEN_MODEL_ID
    assert MetaSamAdapter.model_id == SAM31_MODEL_ID


def test_adapters_unconfigured_without_keys(monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)
    assert BasetenMuseAdapter().is_configured is False
    assert MetaSamAdapter().is_configured is False


def test_adapters_read_env_keys(monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "bt-test")
    monkeypatch.setenv("META_API_KEY", "meta-test")
    assert BasetenMuseAdapter().is_configured is True
    assert MetaSamAdapter().is_configured is True


def test_live_analysis_refuses_without_key(monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)
    with pytest.raises(ProviderNotConfigured, match="BASETEN_API_KEY"):
        BasetenMuseAdapter().analyze(muse_request(), client=mock_client(lambda r: None))
    with pytest.raises(ProviderNotConfigured, match="META_API_KEY"):
        MetaSamAdapter().analyze(sam_request(), client=mock_client(lambda r: None))


def test_require_configured_raises_without_key(monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    with pytest.raises(ProviderNotConfigured):
        BasetenMuseAdapter().require_configured()


def test_http_error_mapping():
    adapter = BasetenMuseAdapter(api_key="x")
    assert isinstance(adapter.map_http_error(401, "bad"), ProviderAuthError)
    assert isinstance(adapter.map_http_error(429, "slow"), ProviderRateLimitError)
    assert adapter.map_http_error(500, "boom").status == 500
    assert isinstance(ProviderResponseError("x"), Exception)


def test_image_attachment_rejects_bad_inputs():
    with pytest.raises(ValueError, match="MIME"):
        ImageAttachment(photo_id="p", mime_type="image/gif", data=b"x")
    with pytest.raises(ValueError, match="empty"):
        ImageAttachment(photo_id="p", mime_type="image/png", data=b"")
    assert attachment().data_url().startswith("data:image/png;base64,")


def test_request_payload_builders_are_pure():
    muse = muse_request().to_payload()
    assert muse["model"] == BASETEN_MODEL_ID
    assert muse["metadata"]["photo_ids"] == "p1"
    assert muse["response_format"] == {"type": "json_object"}
    parts = muse["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "u"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")

    sam = sam_request().to_payload()
    assert sam["model"] == SAM31_MODEL_ID
    assert sam["stream"] is True
    assert sam["metadata"] == {"mask_encoding": "one_bit"}
    content = sam["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "steering wheel button"}
    assert content[1]["image_url"].startswith("data:image/png;base64,")


def test_sam_request_validation():
    with pytest.raises(ValueError, match="short noun phrase"):
        sam_request(concept="this concept has far too many words to be valid here")
    with pytest.raises(ValueError, match="forbidden"):
        sam_request(concept="button. next sentence")
    with pytest.raises(ValueError, match="out of bounds"):
        sam_request(concept=None, boxes=((0, 0, 9999, 10),))
    with pytest.raises(ValueError, match="required"):
        sam_request(concept=None)
    boxed = sam_request(concept=None, boxes=((1, 2, 3, 4),))
    assert boxed.prompt_text() == "<0f>0<|box;x1=1;y1=2;x2=2;y2=3;w=160;h=90|>"
    with pytest.raises(ValueError, match="exactly one of"):
        sam_request(boxes=((1, 2, 3, 4),))


def test_muse_observation_validator():
    good = {
        "photo_id": "p1",
        "claim_concept": "adaptive cruise control",
        "visible_object": "steering wheel button",
        "description": "a button with a car icon",
        "supports_localization": True,
        "ambiguous": False,
        "needs_crop": False,
        "needs_other_view": True,
    }
    observations, warnings = validate_observation_payload(
        {"observations": [good]}, frozenset({"p1"})
    )
    assert observations[0]["notes"] == ""
    assert warnings == []

    observations, warnings = validate_observation_payload(
        {"observations": []}, frozenset({"p1"})
    )
    assert observations == []

    with pytest.raises(ProviderResponseError, match="lacks keys"):
        validate_observation_payload(
            {"observations": [{"photo_id": "p1"}]}, frozenset({"p1"})
        )
    bad_bool = dict(good, ambiguous="yes")
    with pytest.raises(ProviderResponseError, match="not a boolean"):
        validate_observation_payload({"observations": [bad_bool]}, frozenset({"p1"}))
    bad_photo = dict(good, photo_id="nope")
    with pytest.raises(ProviderResponseError, match="unknown photo_id"):
        validate_observation_payload({"observations": [bad_photo]}, frozenset({"p1"}))
    extra = dict(good, surprise=1)
    _, warnings = validate_observation_payload(
        {"observations": [extra]}, frozenset({"p1"})
    )
    assert any("surprise" in warning for warning in warnings)


def test_muse_analyze_success_records_usage():
    body = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "observations": [
                                {
                                    "photo_id": "p1",
                                    "claim_concept": "c",
                                    "visible_object": "v",
                                    "description": "d",
                                    "supports_localization": False,
                                    "ambiguous": True,
                                    "needs_crop": False,
                                    "needs_other_view": False,
                                }
                            ]
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer bt-test"
        payload = json.loads(request.content)
        assert payload["model"] == BASETEN_MODEL_ID
        return httpx.Response(200, json=body)

    adapter = BasetenMuseAdapter(api_key="bt-test")
    result = adapter.analyze(
        muse_request(), client=mock_client(handler), sleep=lambda s: None
    )
    assert isinstance(result, MuseObservationResult)
    assert len(result.observations) == 1
    assert adapter.live_calls_attempted == 1
    (record,) = adapter.ledger.records
    assert record.status == "ok"
    assert record.input_tokens == 1000
    assert record.output_tokens == 100
    assert record.estimated_cost_usd == pytest.approx(
        1000 / 1e6 * 1.25 + 100 / 1e6 * 4.25
    )
    assert record.pricing_version == BASETEN_PRICING_VERSION
    assert record.retries == 0


def test_muse_analyze_rejects_malformed_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "nope"}}]})

    adapter = BasetenMuseAdapter(api_key="bt-test")
    with pytest.raises(ProviderResponseError, match="not JSON"):
        adapter.analyze(
            muse_request(), client=mock_client(handler), sleep=lambda s: None
        )


def test_muse_analyze_retries_rate_limit_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, text="slow")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps({"observations": []})}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    adapter = BasetenMuseAdapter(api_key="bt-test")
    result = adapter.analyze(
        muse_request(), client=mock_client(handler), sleep=lambda s: None
    )
    assert result.observations == ()
    assert adapter.live_calls_attempted == 2
    assert adapter.ledger.records[0].retries == 1


def test_muse_analyze_does_not_retry_auth_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    adapter = BasetenMuseAdapter(api_key="bad")
    with pytest.raises(ProviderAuthError):
        adapter.analyze(
            muse_request(), client=mock_client(handler), sleep=lambda s: None
        )
    assert adapter.live_calls_attempted == 1
    assert adapter.ledger.records[0].status == "error"


def sse_stream(text: str) -> httpx.Response:
    delta = json.dumps({"type": "response.output_text.delta", "delta": text})
    lines = "\n".join([f"data: {delta}", 'data: {"type": "response.completed"}', ""])
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=lines.encode()
    )


def one_bit_payload(width: int, height: int, pixels: bytes) -> str:
    # Fixture scaffolding only: Meta's own encoder builds a canonical
    # payload so tests exercise our grammar + integration, not their codec.
    from meta_sam_parser._mask_codec import _encode_raster

    return _encode_raster(pixels, width, height)


def test_sse_parser_requires_completion():
    assert parse_sse_output_text(iter(['data: {"type": "response.completed"}'])) == ""
    with pytest.raises(ProviderResponseError, match="without completion"):
        parse_sse_output_text(
            iter(['data: {"type": "response.output_text.delta", "delta": "x"}'])
        )
    with pytest.raises(ProviderError, match="failed"):
        parse_sse_output_text(
            iter(['data: {"type": "response.failed", "error": {"message": "boom"}}'])
        )


def test_segment_records_reject_bad_geometry():
    payload = one_bit_payload(4, 4, bytes([1] * 16))
    good = f"<0f>7<|box;x1=1;y1=2;x2=3;y2=4;w=160;h=90|><|mask;x=0;y=0;data=4,4,{payload}|>"
    (record,) = parse_segmentation_records(good, 160, 90)
    assert record.object_id == 7
    assert record.box == (1, 2, 3, 4)
    assert len(record.raster) == 16
    assert record.encoding == "one_bit"

    assert parse_segmentation_records("  ", 160, 90) == []

    drifted = good.replace("w=160;h=90", "w=161;h=90")
    with pytest.raises(ProviderResponseError, match="drifts"):
        parse_segmentation_records(drifted, 160, 90)
    oob = good.replace("x2=3", "x2=999")
    with pytest.raises(ProviderResponseError, match="out of bounds"):
        parse_segmentation_records(oob, 160, 90)
    with pytest.raises(ProviderResponseError, match="unexpected text"):
        parse_segmentation_records("hello", 160, 90)


def test_sam_analyze_success_and_cost():
    payload = one_bit_payload(4, 4, bytes([1] * 16))
    text = f"<0f>3<|box;x1=0;y1=0;x2=9;y2=9;w=160;h=90|><|mask;x=0;y=0;data=4,4,{payload}|>"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        assert request.headers["authorization"] == "Bearer meta-test"
        body = json.loads(request.content)
        assert body["model"] == SAM31_MODEL_ID
        assert body["stream"] is True
        return sse_stream(text)

    adapter = MetaSamAdapter(api_key="meta-test")
    result = adapter.analyze(
        sam_request(), client=mock_client(handler), sleep=lambda s: None
    )
    assert len(result.records) == 1
    assert result.records[0].object_id == 3
    (record,) = adapter.ledger.records
    assert record.status == "ok"
    assert record.estimated_cost_usd == META_PRICE_PER_IMAGE_USD
    assert record.pricing_version == META_PRICING_VERSION


def test_sam_analyze_zero_match_is_ok():
    adapter = MetaSamAdapter(api_key="meta-test")
    result = adapter.analyze(
        sam_request(concept="red bicycle"),
        client=mock_client(lambda request: sse_stream("")),
        sleep=lambda s: None,
    )
    assert result.records == ()


def test_ledger_keeps_provider_costs_separate():
    ledger = UsageLedger()
    ledger.record(
        UsageRecord(
            provider="baseten",
            model_id=BASETEN_MODEL_ID,
            prompt_version="v1",
            photo_ids=("a",),
            status="ok",
            estimated_cost_usd=0.05,
        )
    )
    ledger.record(
        UsageRecord(
            provider="meta",
            model_id=SAM31_MODEL_ID,
            prompt_version="v1",
            photo_ids=("a",),
            status="ok",
            estimated_cost_usd=0.02,
        )
    )
    assert ledger.cost_by_provider() == {"baseten": 0.05, "meta": 0.02}
