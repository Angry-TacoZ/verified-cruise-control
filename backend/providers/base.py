"""Shared provider contracts: errors, usage records, adapter interface."""

from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ProviderError(Exception):
    """Base for all structured provider failures."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ProviderNotConfigured(ProviderError):
    """Raised when a live call is attempted without credentials/enablement."""


class ProviderAuthError(ProviderError):
    """401/403 from the provider: bad, missing, or unauthorized key."""


class ProviderRateLimitError(ProviderError):
    """429 from the provider: retry after backoff."""


class ProviderTimeoutError(ProviderError):
    """Client-side timeout waiting for the provider."""


class ProviderResponseError(ProviderError):
    """Provider answered but the payload failed validation."""


@dataclass(frozen=True)
class UsageRecord:
    """One inference execution's attribution. Costs stay separable by pool."""

    provider: str  # "baseten" | "meta"
    model_id: str
    prompt_version: str
    photo_ids: tuple[str, ...]
    status: str  # "ok" | "error"
    latency_ms: int | None = None
    retries: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    pricing_version: str | None = None
    error: str | None = None
    audit_job_id: str | None = None


@dataclass
class PriceTable:
    """Per-1k-token prices for one provider's credit pool (USD)."""

    provider: str
    input_per_1k: float
    output_per_1k: float


class UsageLedger:
    """In-memory usage sink for PR #1 (durable job records land in PR #2)."""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    def record(self, entry: UsageRecord) -> None:
        self.records.append(entry)

    def cost_by_provider(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for entry in self.records:
            totals[entry.provider] = totals.get(entry.provider, 0.0) + (
                entry.estimated_cost_usd or 0.0
            )
        return totals


@dataclass
class RetryPolicy:
    max_retries: int = 2
    base_backoff_s: float = 1.0
    max_backoff_s: float = 10.0


MIME_ALLOWLIST = frozenset({"image/jpeg", "image/png", "image/webp"})


@dataclass(frozen=True)
class ImageAttachment:
    """One preserved-evidence image for a provider request.

    Providers receive backend-validated bytes only (never uploader-supplied
    paths or external URLs). Transport is a base64 data URL so local
    originals never need public hosting.
    """

    photo_id: str
    mime_type: str
    data: bytes

    def __post_init__(self) -> None:
        if not self.photo_id:
            raise ValueError("photo_id is required")
        if self.mime_type not in MIME_ALLOWLIST:
            raise ValueError(f"unsupported image MIME type: {self.mime_type}")
        if not self.data:
            raise ValueError("image data is empty")

    def data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.mime_type};base64,{encoded}"


class ProviderAdapter(ABC):
    """Interface every model provider implements. No network in PR #1."""

    provider_name: str = ""
    model_id: str = ""
    api_base: str = ""
    env_key_name: str = ""
    timeout_s: float = 60.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)  # type: ignore[assignment]

    def __init__(
        self,
        api_key: str | None = None,
        *,
        ledger: UsageLedger | None = None,
        model_id: str | None = None,
    ) -> None:
        self._api_key = (
            api_key if api_key is not None else os.environ.get(self.env_key_name, "")
        )
        self.ledger = ledger or UsageLedger()
        if model_id is not None:
            self.model_id = model_id
        self.live_calls_attempted = 0

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def require_configured(self) -> None:
        if not self.is_configured:
            raise ProviderNotConfigured(
                f"{self.provider_name} adapter has no {self.env_key_name}; "
                "set it to enable live inference."
            )

    @abstractmethod
    def analyze(self, *args, **kwargs):
        """Run live inference. PR #1 implementations always raise."""

    def map_http_error(self, status: int, body: str) -> ProviderError:
        if status in (401, 403):
            return ProviderAuthError(f"{self.provider_name} auth failed", status=status)
        if status == 429:
            return ProviderRateLimitError(
                f"{self.provider_name} rate limited", status=status
            )
        if status == 408:
            return ProviderTimeoutError(
                f"{self.provider_name} timed out", status=status
            )
        return ProviderError(
            f"{self.provider_name} error {status}: {body[:200]}", status=status
        )
