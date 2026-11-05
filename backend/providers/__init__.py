"""Provider adapters (PR #1: contracts only — no live inference).

Each adapter defines the exact model identifier, configuration loading,
timeout/retry policy, structured error contract, and usage/cost attribution
records that PR #2's live integration will implement. Live analysis methods
raise :class:`ProviderNotConfigured`; tests assert the import pipeline makes
zero provider calls.
"""

from .base import (
    ImageAttachment,
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
from .meta import META_API_BASE, SAM31_MODEL_ID, MetaSamAdapter
from .meta_muse import (
    META_MUSE_API_BASE,
    META_MUSE_MODEL_ID,
    META_MUSE_PRICING_VERSION,
    MetaMuseAdapter,
    MuseObservationResult,
    MuseRequest,
    validate_observation_payload,
)

__all__ = [
    "META_API_BASE",
    "META_MUSE_API_BASE",
    "META_MUSE_MODEL_ID",
    "META_MUSE_PRICING_VERSION",
    "SAM31_MODEL_ID",
    "ImageAttachment",
    "MetaMuseAdapter",
    "MetaSamAdapter",
    "MuseObservationResult",
    "MuseRequest",
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
    "validate_observation_payload",
]
