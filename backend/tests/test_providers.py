"""Provider contracts: identifiers, config boundaries, error mapping, ledger.

No test here touches the network. Live analysis must refuse in PR #1.
"""

from __future__ import annotations

import pytest

from providers import (
    BASETEN_API_BASE,
    BASETEN_MODEL_ID,
    META_API_BASE,
    SAM31_MODEL_ID,
    BasetenMuseAdapter,
    MetaSamAdapter,
    ProviderAuthError,
    ProviderNotConfigured,
    ProviderRateLimitError,
    ProviderResponseError,
    UsageLedger,
    UsageRecord,
)
from providers.baseten import MuseRequest
from providers.meta import SamRequest


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


def test_live_analysis_refuses_in_pr1():
    with pytest.raises(ProviderNotConfigured):
        BasetenMuseAdapter(api_key="x").analyze()
    with pytest.raises(ProviderNotConfigured):
        MetaSamAdapter(api_key="x").analyze()


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


def test_request_payload_builders_are_pure():
    muse = MuseRequest(
        prompt_version="v1",
        system_instructions="s",
        user_text="u",
        photo_ids=("a", "b"),
    ).to_payload()
    assert muse["model"] == BASETEN_MODEL_ID
    assert muse["metadata"]["photo_ids"] == ["a", "b"]
    sam = SamRequest(
        photo_id="a", prompt_kind="box", prompt_payload={"box": [1, 2, 3, 4]}
    ).to_payload()
    assert sam["model"] == SAM31_MODEL_ID
    assert sam["prompt"]["kind"] == "box"


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
