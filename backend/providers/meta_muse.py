"""Meta Muse Spark adapter (live inference).

Calls Meta Model API's OpenAI-compatible ``POST {api_base}/chat/completions``
with a Muse Spark model (default ``muse-spark-1.3-contributor``). Images ride
as ``image_url`` data URIs, so local originals never need public hosting.

Pricing (USD per 1M tokens, verified Nov 2026 against Meta's published
rates via promptfoo/LiteLLM/OpenRouter, all converging): input 0.10,
output 0.20, cached input 0.002. The contributor tier permits Meta to train
on prompts/completions; that tradeoff is the operator's explicit choice of
model ID, recorded on every usage row.

Observation validation lives here next to the only adapter that uses it:
the server is never the schema contract, malformed output is quarantined,
and an empty observation list stays valid (a photo may legitimately show
nothing relevant).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx

from .base import (
    ImageAttachment,
    ProviderAdapter,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    RetryPolicy,
    UsageRecord,
)

OBSERVATION_REQUIRED_KEYS = frozenset(
    {
        "photo_id",
        "claim_concept",
        "visible_object",
        "description",
        "supports_localization",
        "ambiguous",
        "needs_crop",
        "needs_other_view",
    }
)
OBSERVATION_BOOL_KEYS = frozenset(
    {
        "supports_localization",
        "ambiguous",
        "needs_crop",
        "needs_other_view",
    }
)
OBSERVATION_SHORT_CAPS = {"claim_concept": 200, "visible_object": 200}
OBSERVATION_LONG_CAPS = {"description": 2000, "notes": 500}
# Layer B (prompt m2): physical-control identification. All optional so
# prompt-m1-shaped responses and older fakes still validate; present
# values are strictly typed and capped like the required keys.
OBSERVATION_OPTIONAL_STR_CAPS = {
    "control_symbol": 200,
    "control_type": 80,
    "location": 40,
    "manufacturer_context": 200,
}
OBSERVATION_OPTIONAL_BOOL_KEYS = frozenset({"dedicated"})
OBSERVATION_CONTROLS_MAX_ITEMS = 4
OBSERVATION_CONTROL_ITEM_CAPS = {
    "visible_object": 200,
    "control_symbol": 200,
    "control_type": 80,
    "location": 40,
}


@dataclass(frozen=True)
class MuseRequest:
    """Pure request payload builder input (testable without network)."""

    prompt_version: str
    system_instructions: str
    user_text: str
    photo_ids: tuple[str, ...]
    images: tuple[ImageAttachment, ...] = ()
    max_output_tokens: int = 2048
    temperature: float = 0.0

    def to_payload(self) -> dict:
        content: list[dict] = [{"type": "text", "text": self.user_text}]
        for image in self.images:
            content.append(
                {"type": "image_url", "image_url": {"url": image.data_url()}}
            )
        return {
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self.system_instructions},
                {"role": "user", "content": content},
            ],
        }


def _truncate(value: str, cap: int, label: str, warnings: list[str]) -> str:
    if len(value) > cap:
        warnings.append(f"{label} truncated to {cap} characters")
        return value[:cap]
    return value


def validate_observation_payload(
    payload: object,
    photo_ids: frozenset[str],
    allowed_concepts: frozenset[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Strictly validate Muse's structured observations.

    Returns ``(observations, warnings)``. Unknown keys are stripped with a
    recorded warning (the server does not enforce the schema); anything
    structurally wrong raises :class:`ProviderResponseError` so the caller
    quarantines the response. An empty observation list is valid: a photo
    may legitimately show nothing relevant. When ``allowed_concepts`` is
    given, every claim_concept must be a requested claim ID.
    """
    warnings: list[str] = []
    if not isinstance(payload, dict):
        raise ProviderResponseError("Muse response is not a JSON object")
    raw = payload.get("observations")
    if not isinstance(raw, list):
        raise ProviderResponseError("Muse response lacks an 'observations' list")
    observations: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ProviderResponseError(f"Muse observation {index} is not an object")
        missing = [key for key in OBSERVATION_REQUIRED_KEYS if key not in item]
        if missing:
            raise ProviderResponseError(
                f"Muse observation {index} lacks keys: {', '.join(missing)}"
            )
        for key in OBSERVATION_BOOL_KEYS:
            if not isinstance(item[key], bool):
                raise ProviderResponseError(
                    f"Muse observation {index} key '{key}' is not a boolean"
                )
        photo_id = item["photo_id"]
        if not isinstance(photo_id, str) or photo_id not in photo_ids:
            raise ProviderResponseError(
                f"Muse observation {index} has an unknown photo_id"
            )
        if (
            allowed_concepts is not None
            and item["claim_concept"] not in allowed_concepts
        ):
            raise ProviderResponseError(
                f"Muse observation {index} has an unknown claim_concept"
            )
        clean: dict = {"photo_id": photo_id}
        for key in ("claim_concept", "visible_object", "description"):
            value = item[key]
            if not isinstance(value, str) or not value.strip():
                raise ProviderResponseError(
                    f"Muse observation {index} key '{key}' is not a non-empty string"
                )
            cap = OBSERVATION_SHORT_CAPS.get(key, OBSERVATION_LONG_CAPS["description"])
            clean[key] = _truncate(value, cap, f"observation {index} '{key}'", warnings)
        for key in OBSERVATION_BOOL_KEYS:
            clean[key] = item[key]
        notes = item.get("notes", "")
        if not isinstance(notes, str):
            raise ProviderResponseError(
                f"Muse observation {index} key 'notes' is not a string"
            )
        clean["notes"] = _truncate(
            notes,
            OBSERVATION_LONG_CAPS["notes"],
            f"observation {index} 'notes'",
            warnings,
        )
        for key, cap in OBSERVATION_OPTIONAL_STR_CAPS.items():
            if key not in item:
                continue
            value = item[key]
            if not isinstance(value, str):
                raise ProviderResponseError(
                    f"Muse observation {index} key '{key}' is not a string"
                )
            clean[key] = _truncate(value, cap, f"observation {index} '{key}'", warnings)
        for key in OBSERVATION_OPTIONAL_BOOL_KEYS:
            if key not in item:
                continue
            if not isinstance(item[key], bool):
                raise ProviderResponseError(
                    f"Muse observation {index} key '{key}' is not a boolean"
                )
            clean[key] = item[key]
        if "controls" in item:
            clean["controls"] = _validate_controls(item["controls"], index, warnings)
        known = (
            set(OBSERVATION_REQUIRED_KEYS)
            | {"notes", "controls"}
            | set(OBSERVATION_OPTIONAL_STR_CAPS)
            | set(OBSERVATION_OPTIONAL_BOOL_KEYS)
        )
        for key in sorted(item.keys()):
            if key not in known:
                warnings.append(f"observation {index} dropped unknown key '{key}'")
        observations.append(clean)
    return observations, warnings


def _validate_controls(controls: object, index: int, warnings: list[str]) -> list[dict]:
    """Validate the optional nested distinct-controls array."""
    if not isinstance(controls, list):
        raise ProviderResponseError(
            f"Muse observation {index} key 'controls' is not a list"
        )
    if len(controls) > OBSERVATION_CONTROLS_MAX_ITEMS:
        raise ProviderResponseError(
            f"Muse observation {index} lists more than "
            f"{OBSERVATION_CONTROLS_MAX_ITEMS} controls"
        )
    clean_items = []
    for position, entry in enumerate(controls):
        where = f"observation {index} controls[{position}]"
        if not isinstance(entry, dict):
            raise ProviderResponseError(f"Muse {where} is not an object")
        visible = entry.get("visible_object")
        if not isinstance(visible, str) or not visible.strip():
            raise ProviderResponseError(
                f"Muse {where} lacks a non-empty 'visible_object'"
            )
        clean_entry: dict = {}
        for key, cap in OBSERVATION_CONTROL_ITEM_CAPS.items():
            if key not in entry:
                continue
            value = entry[key]
            if not isinstance(value, str):
                raise ProviderResponseError(f"Muse {where} key '{key}' is not a string")
            clean_entry[key] = _truncate(value, cap, f"{where} '{key}'", warnings)
        for key in sorted(entry.keys()):
            if key not in OBSERVATION_CONTROL_ITEM_CAPS:
                warnings.append(f"Muse {where} dropped unknown key '{key}'")
        clean_items.append(clean_entry)
    return clean_items


@dataclass(frozen=True)
class MuseObservationResult:
    """Validated Muse output plus its usage record."""

    observations: tuple[dict, ...]
    model: str
    prompt_version: str
    usage: UsageRecord
    validation_warnings: tuple[str, ...] = ()


META_MUSE_MODEL_ID = "muse-spark-1.3-contributor"
META_MUSE_API_BASE = "https://api.meta.ai/v1"
META_MUSE_PRICING_VERSION = "meta-muse-2026-11-04"
META_MUSE_INPUT_USD_PER_1M = 0.10
META_MUSE_OUTPUT_USD_PER_1M = 0.20


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def validate_triage_payload(
    payload: object, ordered_ids: list[str]
) -> tuple[list[str], list[str]]:
    """Strictly validate a triage response: every screened photo id appears
    exactly once with a boolean verdict. Anything else raises
    :class:`ProviderResponseError` so the caller falls back to the full
    photo set instead of silently auditing a subset.
    """
    if not isinstance(payload, dict):
        raise ProviderResponseError("Meta Muse triage response is not a JSON object")
    relevant = payload.get("relevant")
    if not isinstance(relevant, dict):
        raise ProviderResponseError("Meta Muse triage response lacks a 'relevant' map")
    wanted = set(ordered_ids)
    got = set(relevant)
    if got != wanted:
        raise ProviderResponseError(
            f"Meta Muse triage covers {len(got)} of {len(wanted)} photos; "
            "falling back to the full set"
        )
    bad = sorted(key for key, value in relevant.items() if not isinstance(value, bool))
    if bad:
        raise ProviderResponseError(
            f"Meta Muse triage has {len(bad)} non-boolean verdict(s); "
            "falling back to the full set"
        )
    return [pid for pid in ordered_ids if relevant[pid]], []


@dataclass(frozen=True)
class TriageResult:
    """Validated triage output plus its usage record."""

    included: tuple[str, ...]
    model: str
    prompt_version: str
    usage: UsageRecord
    validation_warnings: tuple[str, ...] = ()


class MetaMuseAdapter(ProviderAdapter):
    provider_name = "meta"
    model_id = META_MUSE_MODEL_ID
    api_base = META_MUSE_API_BASE
    env_key_name = "META_API_KEY"
    timeout_s = 90.0
    retry_policy = RetryPolicy(max_retries=2)

    def _check_request(self, request: MuseRequest) -> None:
        self.require_configured()
        photo_ids = frozenset(request.photo_ids)
        if not photo_ids:
            raise ValueError("MuseRequest needs at least one photo_id")
        if {image.photo_id for image in request.images} - photo_ids:
            raise ValueError("MuseRequest images must match declared photo_ids")

    def _post(
        self,
        request: MuseRequest,
        *,
        client: httpx.Client | None = None,
        sleep=_sleep,
    ) -> tuple[httpx.Response, int, float]:
        """POST one chat-completion request with bounded retries.

        Returns ``(response, retries, started)`` on HTTP 200; otherwise
        raises the last :class:`ProviderError`.
        """
        policy = self.retry_policy
        retries = 0
        last_error: ProviderError | None = None
        started = time.monotonic()
        owned_client = (
            client if client is not None else httpx.Client(timeout=self.timeout_s)
        )
        # Correlation stays client-side: request photo_ids + ledger rows.
        payload = request.to_payload()
        try:
            for attempt in range(policy.max_retries + 1):
                self.live_calls_attempted += 1
                try:
                    response = owned_client.post(
                        f"{self.api_base}/chat/completions",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json={**payload, "model": self.model_id},
                    )
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    last_error = ProviderTimeoutError(
                        f"meta request failed: {exc.__class__.__name__}"
                    )
                else:
                    if response.status_code == 200:
                        self.last_attempts = retries + 1
                        return response, retries, started
                    last_error = self.map_http_error(
                        response.status_code, response.text
                    )
                if attempt >= policy.max_retries or not self._retryable(last_error):
                    break
                retries += 1
                backoff = min(
                    policy.base_backoff_s * (2**attempt), policy.max_backoff_s
                )
                sleep(backoff)
        finally:
            if client is None:
                owned_client.close()
        assert last_error is not None
        self.last_attempts = retries + 1
        self._record(request, retries, started, None, None, None, last_error)
        raise last_error

    def analyze(
        self,
        request: MuseRequest,
        *,
        client: httpx.Client | None = None,
        sleep=_sleep,
    ) -> MuseObservationResult:
        """Run one Muse observation call with bounded retries."""
        self._check_request(request)
        response, retries, started = self._post(request, client=client, sleep=sleep)
        return self._handle_success(request, response, retries, started)

    def analyze_triage(
        self,
        request: MuseRequest,
        *,
        client: httpx.Client | None = None,
        sleep=_sleep,
    ) -> TriageResult:
        """Run one photo-triage call with bounded retries.

        The thumbnails ride as ``images``; the validated result lists which
        screened photo ids merit full observation. Malformed output raises
        :class:`ProviderResponseError` so the caller falls back to all
        photos instead of auditing a corrupt subset.
        """
        self._check_request(request)
        response, retries, started = self._post(request, client=client, sleep=sleep)
        return self._handle_triage(request, response, retries, started)

    def _handle_triage(
        self,
        request: MuseRequest,
        response: httpx.Response,
        retries: int,
        started: float,
    ) -> TriageResult:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(
                f"Meta Muse triage has no usable message content: {exc}"
            ) from exc
        if not isinstance(content, str):
            raise ProviderResponseError("Meta Muse triage message content is not text")
        try:
            payload = json.loads(content)
        except ValueError as exc:
            raise ProviderResponseError(
                f"Meta Muse triage content is not JSON: {exc}"
            ) from exc
        included, warnings = validate_triage_payload(payload, list(request.photo_ids))
        usage_block = body.get("usage") if isinstance(body, dict) else None
        input_tokens = self._token_count(usage_block, "prompt_tokens")
        output_tokens = self._token_count(usage_block, "completion_tokens")
        usage = self._record(
            request, retries, started, input_tokens, output_tokens, None, None
        )
        return TriageResult(
            included=tuple(included),
            model=self.model_id,
            prompt_version=request.prompt_version,
            usage=usage,
            validation_warnings=tuple(warnings),
        )

    @staticmethod
    def _retryable(error: ProviderError | None) -> bool:
        if isinstance(error, (ProviderTimeoutError, ProviderRateLimitError)):
            return True
        return error is not None and error.status is not None and error.status >= 500

    def _handle_success(
        self,
        request: MuseRequest,
        response: httpx.Response,
        retries: int,
        started: float,
    ) -> MuseObservationResult:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(
                f"Meta Muse response has no usable message content: {exc}"
            ) from exc
        if not isinstance(content, str):
            raise ProviderResponseError("Meta Muse message content is not text")
        choice = body["choices"][0]
        finish_reason = (
            choice.get("finish_reason") if isinstance(choice, dict) else None
        )
        if finish_reason == "length":
            raise ProviderResponseError(
                "Meta Muse response hit the output-token limit; observations "
                "are incomplete"
            )
        try:
            payload = json.loads(content)
        except ValueError as exc:
            raise ProviderResponseError(
                f"Meta Muse message content is not JSON: {exc}"
            ) from exc
        observations, warnings = validate_observation_payload(
            payload, frozenset(request.photo_ids)
        )
        usage_block = body.get("usage") if isinstance(body, dict) else None
        input_tokens = self._token_count(usage_block, "prompt_tokens")
        output_tokens = self._token_count(usage_block, "completion_tokens")
        usage = self._record(
            request, retries, started, input_tokens, output_tokens, None, None
        )
        return MuseObservationResult(
            observations=tuple(observations),
            model=self.model_id,
            prompt_version=request.prompt_version,
            usage=usage,
            validation_warnings=tuple(warnings),
        )

    @staticmethod
    def _token_count(usage_block: object, key: str) -> int | None:
        if not isinstance(usage_block, dict):
            return None
        value = usage_block.get(key)
        return value if isinstance(value, int) and value >= 0 else None

    def _record(
        self,
        request: MuseRequest,
        retries: int,
        started: float,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_override: float | None,
        error: ProviderError | None,
    ) -> UsageRecord:
        latency_ms = int((time.monotonic() - started) * 1000)
        cost: float | None = None
        if input_tokens is not None and output_tokens is not None:
            cost = (
                input_tokens / 1_000_000 * META_MUSE_INPUT_USD_PER_1M
                + output_tokens / 1_000_000 * META_MUSE_OUTPUT_USD_PER_1M
            )
        record = UsageRecord(
            provider=self.provider_name,
            model_id=self.model_id,
            prompt_version=request.prompt_version,
            photo_ids=request.photo_ids,
            status="error" if error else "ok",
            latency_ms=latency_ms,
            retries=retries,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost_override if cost_override is not None else cost,
            pricing_version=META_MUSE_PRICING_VERSION,
            error=str(error) if error else None,
        )
        self.ledger.record(record)
        return record
