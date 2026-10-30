"""Provider adapters (PR #1: contracts only — no live inference).

Each adapter defines the exact model identifier, configuration loading,
timeout/retry policy, structured error contract, and usage/cost attribution
records that PR #2's live integration will implement. Live analysis methods
raise :class:`ProviderNotConfigured`; tests assert the import pipeline makes
zero provider calls.
"""

from .base import (
    PriceTable,
    ProviderAdapter,
    ProviderAuthError,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    UsageLedger,
    UsageRecord,
)
from .baseten import BASETEN_API_BASE, BASETEN_MODEL_ID, BasetenMuseAdapter
from .meta import META_API_BASE, SAM31_MODEL_ID, MetaSamAdapter

__all__ = [
    "BASETEN_API_BASE",
    "BASETEN_MODEL_ID",
    "META_API_BASE",
    "SAM31_MODEL_ID",
    "BasetenMuseAdapter",
    "MetaSamAdapter",
    "PriceTable",
    "ProviderAdapter",
    "ProviderAuthError",
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderRateLimitError",
    "ProviderResponseError",
    "ProviderTimeoutError",
    "UsageLedger",
    "UsageRecord",
]
