"""Baseten Muse Spark Open 1026 adapter contract (PR #1: no live calls)."""

from __future__ import annotations

from dataclasses import dataclass

from .base import ProviderAdapter, ProviderNotConfigured, RetryPolicy

BASETEN_MODEL_ID = "meta-models/muse-spark-open-1026"
BASETEN_API_BASE = "https://inference.baseten.co/v1"


@dataclass(frozen=True)
class MuseRequest:
    """Pure request payload builder input (testable without network)."""

    prompt_version: str
    system_instructions: str
    user_text: str
    photo_ids: tuple[str, ...]
    max_output_tokens: int = 2048
    temperature: float = 0.0

    def to_payload(self) -> dict:
        return {
            "model": BASETEN_MODEL_ID,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "metadata": {
                "prompt_version": self.prompt_version,
                "photo_ids": list(self.photo_ids),
            },
            "messages": [
                {"role": "system", "content": self.system_instructions},
                {"role": "user", "content": self.user_text},
            ],
        }


class BasetenMuseAdapter(ProviderAdapter):
    provider_name = "baseten"
    model_id = BASETEN_MODEL_ID
    api_base = BASETEN_API_BASE
    env_key_name = "BASETEN_API_KEY"
    timeout_s = 90.0
    retry_policy = RetryPolicy(max_retries=2)

    def analyze(self, *args, **kwargs):
        self.live_calls_attempted += 1
        raise ProviderNotConfigured(
            "Baseten Muse Spark inference is not implemented until PR #2."
        )
