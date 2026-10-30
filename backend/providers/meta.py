"""Meta SAM 3.1 adapter contract (PR #1: no live calls)."""

from __future__ import annotations

from dataclasses import dataclass

from .base import ProviderAdapter, ProviderNotConfigured, RetryPolicy

SAM31_MODEL_ID = "sam-3.1"
META_API_BASE = "https://api.meta.ai/v1"


@dataclass(frozen=True)
class SamRequest:
    """Pure request payload builder input (testable without network)."""

    photo_id: str
    prompt_kind: str  # "point" | "box" | "auto"
    prompt_payload: dict
    multimask: bool = True

    def to_payload(self) -> dict:
        return {
            "model": SAM31_MODEL_ID,
            "photo_id": self.photo_id,
            "prompt": {"kind": self.prompt_kind, **self.prompt_payload},
            "multimask": self.multimask,
        }


class MetaSamAdapter(ProviderAdapter):
    provider_name = "meta"
    model_id = SAM31_MODEL_ID
    api_base = META_API_BASE
    env_key_name = "META_API_KEY"
    timeout_s = 60.0
    retry_policy = RetryPolicy(max_retries=3)

    def analyze(self, *args, **kwargs):
        self.live_calls_attempted += 1
        raise ProviderNotConfigured(
            "Meta SAM 3.1 inference is not implemented until PR #2."
        )
