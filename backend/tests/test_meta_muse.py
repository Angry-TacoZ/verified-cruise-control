"""Meta Muse Spark adapter: identifiers, config, payloads, errors.

No test here touches the network: live paths run against httpx
MockTransport. Costs follow Meta's published contributor rates
($0.10/1M input, $0.20/1M output).
"""

from __future__ import annotations

import json

import httpx
import pytest

from providers import META_MUSE_API_BASE, META_MUSE_MODEL_ID, MetaMuseAdapter
from providers.base import (
    ImageAttachment,
    ProviderAuthError,
    ProviderError,
    ProviderNotConfigured,
    ProviderResponseError,
)
from providers.meta_muse import (
    META_MUSE_INPUT_USD_PER_1M,
    META_MUSE_OUTPUT_USD_PER_1M,
    META_MUSE_PRICING_VERSION,
    MuseRequest,
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


def valid_observation(photo_id: str = "p1") -> dict:
    return {
        "photo_id": photo_id,
        "claim_concept": "adaptive-cruise",
        "visible_object": "steering wheel button",
        "description": "a button on the wheel",
        "supports_localization": True,
        "ambiguous": False,
        "needs_crop": False,
        "needs_other_view": False,
    }


def ok_body(*, observations=None, usage=None) -> dict:
    return {
        "id": "chatcmpl-test",
        "model": META_MUSE_MODEL_ID,
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "observations": observations
                            if observations is not None
                            else []
                        }
                    )
                },
                "finish_reason": "stop",
            }
        ],
        "usage": usage
        if usage is not None
        else {"prompt_tokens": 100, "completion_tokens": 50},
    }


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_exact_model_identifiers():
    assert META_MUSE_MODEL_ID == "muse-spark-1.3-contributor"
    assert META_MUSE_API_BASE == "https://api.meta.ai/v1"
    assert MetaMuseAdapter.model_id == META_MUSE_MODEL_ID
    assert META_MUSE_PRICING_VERSION == "meta-muse-2026-11-04"
    assert META_MUSE_INPUT_USD_PER_1M == 0.10
    assert META_MUSE_OUTPUT_USD_PER_1M == 0.20


def test_unconfigured_without_key(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    assert MetaMuseAdapter().is_configured is False
    with pytest.raises(ProviderNotConfigured):
        MetaMuseAdapter().analyze(muse_request())


def test_configured_with_key(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "mk-test")
    assert MetaMuseAdapter().is_configured is True


def test_posts_to_chat_completions_with_model_override():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json=ok_body())

    adapter = MetaMuseAdapter(api_key="mk-test", model_id="custom-model")
    result = adapter.analyze(
        muse_request(), client=mock_client(handler), sleep=lambda s: None
    )
    assert seen["url"] == "https://api.meta.ai/v1/chat/completions"
    assert seen["payload"]["model"] == "custom-model"
    assert seen["payload"]["response_format"] == {"type": "json_object"}
    image_parts = [
        p
        for p in seen["payload"]["messages"][-1]["content"]
        if p.get("type") == "image_url"
    ]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert result.observations == ()


def test_happy_path_observations_and_cost_math():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=ok_body(
                observations=[valid_observation()],
                usage={"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            ),
        )

    adapter = MetaMuseAdapter(api_key="mk-test")
    result = adapter.analyze(
        muse_request(), client=mock_client(handler), sleep=lambda s: None
    )
    assert len(result.observations) == 1
    assert result.observations[0]["claim_concept"] == "adaptive-cruise"
    assert result.model == META_MUSE_MODEL_ID
    assert result.usage.input_tokens == 1_000_000
    assert result.usage.output_tokens == 1_000_000
    assert result.usage.estimated_cost_usd == pytest.approx(0.30)
    assert result.usage.pricing_version == META_MUSE_PRICING_VERSION
    assert result.usage.provider == "meta"


def test_auth_failure_maps_to_provider_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    adapter = MetaMuseAdapter(api_key="bad")
    with pytest.raises(ProviderAuthError, match="meta auth failed"):
        adapter.analyze(
            muse_request(), client=mock_client(handler), sleep=lambda s: None
        )


def test_triage_request_omits_metadata_block():
    # The Meta API caps metadata values at 512 chars; a 31-photo triage
    # request carries ~1.1k chars of photo ids, so the adapter must not
    # send any provider metadata block (live 400 observed 2026-11-05).
    ids = [f"p{i}" for i in range(31)]
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-triage",
                "model": META_MUSE_MODEL_ID,
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"relevant": {pid: True for pid in ids}}
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4000, "completion_tokens": 100},
            },
        )

    req = muse_request(
        prompt_version="triage-v1",
        photo_ids=tuple(ids),
        images=tuple(attachment(pid) for pid in ids),
    )
    adapter = MetaMuseAdapter(api_key="mk-test")
    result = adapter.analyze_triage(
        req, client=mock_client(handler), sleep=lambda s: None
    )
    assert "metadata" not in seen["payload"]
    assert len(result.included) == 31
    assert result.usage.provider == "meta"


def test_rate_limit_retries_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=ok_body())

    adapter = MetaMuseAdapter(api_key="mk-test")
    adapter.analyze(muse_request(), client=mock_client(handler), sleep=lambda s: None)
    assert len(calls) == 2


def test_server_error_raises_after_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    adapter = MetaMuseAdapter(api_key="mk-test")
    with pytest.raises(ProviderError):
        adapter.analyze(
            muse_request(), client=mock_client(handler), sleep=lambda s: None
        )


def test_non_json_content_is_quarantined():
    def handler(request: httpx.Request) -> httpx.Response:
        body = ok_body()
        body["choices"][0]["message"]["content"] = "not json"
        return httpx.Response(200, json=body)

    adapter = MetaMuseAdapter(api_key="mk-test")
    with pytest.raises(ProviderResponseError, match="not JSON"):
        adapter.analyze(
            muse_request(), client=mock_client(handler), sleep=lambda s: None
        )


def test_output_limit_failure_is_exact():
    def handler(request: httpx.Request) -> httpx.Response:
        body = ok_body()
        body["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=body)

    adapter = MetaMuseAdapter(api_key="mk-test")
    with pytest.raises(ProviderResponseError, match="output-token limit"):
        adapter.analyze(
            muse_request(), client=mock_client(handler), sleep=lambda s: None
        )
