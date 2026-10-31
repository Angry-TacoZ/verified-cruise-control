"""Meta SAM 3.1 adapter (PR #2: live inference).

Verified 2026-10-31 against official Meta docs (dev.meta.ai/docs/sam/*):
``POST https://api.meta.ai/v1/responses`` (Responses API, SSE stream to
``response.completed``) with ``model: "sam-3.1"``. The image rides as a
base64 data URL, so local originals never need public hosting. Prompts
are one short concept phrase, optionally with box/point token grammar in
media pixels. Output is the documented token grammar; zero matches
complete with empty output (normal, not an error).

Mask payload decoding uses Meta's official ``meta-sam-parser`` codec (an
arithmetic-coded bitstream — hand-rolling it would be the risky move).
Everything else — SSE handling, record-grammar parsing, bounds checks —
is our own strict layer: malformed responses are quarantined, and
out-of-bounds geometry is rejected, never silently clipped.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
from meta_sam_parser import (
    FramePrompt,
    InvalidSegmentationMaskError,
    PromptBox,
    PromptObject,
    PromptPoint,
    SegmentationMask,
    build_object_prompt,
    decode_mask_to_raster,
)

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

SAM31_MODEL_ID = "sam-3.1"
META_API_BASE = "https://api.meta.ai/v1"
META_PRICING_VERSION = "meta-sam-2026-09-20"
META_PRICE_PER_IMAGE_USD = 2.50 / 1000

MAX_MASK_PIXELS = 64_000_000
MAX_CONCEPT_WORDS = 10
CONCEPT_FORBIDDEN = set(".!?;<>|")

RECORD_HEADER_RE = re.compile(r"<(\d+)f>(\d+)")
GROUP_RE = re.compile(r"<\|(.*?)\|>")


@dataclass(frozen=True)
class SamRequest:
    """Pure request payload builder input (testable without network)."""

    photo: ImageAttachment
    image_width: int
    image_height: int
    concept: str | None = None
    boxes: tuple[tuple[int, int, int, int], ...] = ()
    points: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        if self.image_width <= 0 or self.image_height <= 0:
            raise ValueError("image dimensions must be positive")
        if self.image_width * self.image_height > 40_000_000:
            raise ValueError("image exceeds the 40-megapixel screening cap")
        has_prompts = bool(self.boxes or self.points)
        if (self.concept is None) == (not has_prompts):
            # The API rejects geometric tokens mixed into the noun phrase:
            # prompts travel as a <Nf>-headed object-prompt block instead.
            raise ValueError("exactly one of concept or object prompts is required")
        if self.concept is not None:
            words = self.concept.split()
            if not 1 <= len(words) <= MAX_CONCEPT_WORDS:
                raise ValueError("concept must be a short noun phrase")
            if any(char in CONCEPT_FORBIDDEN for char in self.concept):
                raise ValueError("concept contains forbidden characters")
        for box in self.boxes:
            x1, y1, x2, y2 = box
            if not (
                0 <= x1 < x2 <= self.image_width and 0 <= y1 < y2 <= self.image_height
            ):
                raise ValueError(f"prompt box {box} is out of bounds")
        for point in self.points:
            x, y = point
            if not (0 <= x < self.image_width and 0 <= y < self.image_height):
                raise ValueError(f"prompt point {point} is out of bounds")

    def prompt_text(self) -> str:
        if self.concept is not None:
            return self.concept
        # Boxes are half-open (left, top, right, bottom); points are
        # positive-only. Each box becomes its own object, points share one
        # more object; Meta's builder emits the <Nf>-headed block the API
        # requires (bare geometric tokens are rejected).
        objects = [
            PromptObject(
                id=index,
                prompts=[
                    FramePrompt(
                        frame=0,
                        box=PromptBox(left=x1, top=y1, right=x2, bottom=y2),
                    )
                ],
            )
            for index, (x1, y1, x2, y2) in enumerate(self.boxes)
        ]
        if self.points:
            objects.append(
                PromptObject(
                    id=len(objects),
                    prompts=[
                        FramePrompt(
                            frame=0,
                            points=tuple(PromptPoint(x=x, y=y) for x, y in self.points),
                        )
                    ],
                )
            )
        return build_object_prompt(
            size=(self.image_width, self.image_height), objects=objects
        )

    def to_payload(self) -> dict:
        return {
            "model": SAM31_MODEL_ID,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": self.prompt_text()},
                        {
                            "type": "input_image",
                            "image_url": self.photo.data_url(),
                        },
                    ],
                }
            ],
            "stream": True,
            "metadata": {"mask_encoding": "one_bit"},
        }


@dataclass(frozen=True)
class SegmentRecord:
    """One validated localized object: box, decoded raster, provenance."""

    object_id: int
    box: tuple[int, int, int, int]
    width: int
    height: int
    raster: bytes
    encoding: str
    provider_confidence: float | None = None


@dataclass(frozen=True)
class SamSegmentationResult:
    """Validated SAM output plus its usage record."""

    records: tuple[SegmentRecord, ...]
    model: str
    usage: UsageRecord
    warnings: tuple[str, ...] = ()


def parse_sse_output_text(lines: Iterator[str]) -> str:
    """Fold a Responses SSE stream into its output text.

    Requires ``response.completed``; failures, refusals, and truncation
    raise so the caller quarantines instead of persisting partial output.
    """
    chunks: list[str] = []
    completed = False
    for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError as exc:
            raise ProviderResponseError(
                f"SAM stream has malformed JSON: {exc}"
            ) from exc
        if not isinstance(event, dict):
            raise ProviderResponseError("SAM stream event is not an object")
        kind = event.get("type", "")
        if kind == "response.output_text.delta":
            delta = event.get("delta", "")
            if not isinstance(delta, str):
                raise ProviderResponseError("SAM delta is not text")
            chunks.append(delta)
        elif kind == "response.completed":
            completed = True
        elif kind == "response.failed":
            detail = _event_detail(event)
            raise ProviderError(f"SAM request failed: {detail}")
        elif kind == "response.incomplete":
            raise ProviderResponseError("SAM response is incomplete (truncated)")
        elif "refusal" in kind:
            raise ProviderResponseError("SAM refused the request")
    if not completed:
        raise ProviderResponseError("SAM stream ended without completion")
    return "".join(chunks)


def _event_detail(event: dict) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        return str(error.get("message", error))[:200]
    return str(error or event.get("type", "unknown"))[:200]


def _parse_int(value: str | None, label: str) -> int:
    if value is None or not re.fullmatch(r"\d+", value):
        raise ProviderResponseError(f"SAM record has an invalid {label}")
    return int(value)


def _parse_confidence(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        confidence = float(value)
    except ValueError:
        raise ProviderResponseError("SAM confidence is not numeric") from None
    if not 0.0 <= confidence <= 1.0:
        raise ProviderResponseError("SAM confidence is outside [0, 1]")
    return confidence


def parse_segmentation_records(
    text: str, image_width: int, image_height: int
) -> list[SegmentRecord]:
    """Strictly parse SAM output text into validated records.

    Empty output is a normal zero-match result. Any malformed record —
    bad grammar, dimension drift, out-of-bounds geometry, undecodable
    mask — quarantines the whole response: partial acceptance would risk
    silent evidence corruption.
    """
    stripped = text.strip()
    if not stripped:
        return []
    records: list[SegmentRecord] = []
    seen_ids: set[int] = set()
    pos = 0
    length = len(stripped)
    while True:
        while pos < length and stripped[pos].isspace():
            pos += 1
        if pos >= length:
            break
        header = RECORD_HEADER_RE.match(stripped, pos)
        if header is None:
            raise ProviderResponseError(f"SAM output has unexpected text at {pos}")
        object_id = int(header.group(2))
        if object_id in seen_ids:
            raise ProviderResponseError(f"SAM repeats object id {object_id}")
        seen_ids.add(object_id)
        pos = header.end()
        groups: list[str] = []
        while True:
            while pos < length and stripped[pos].isspace():
                pos += 1
            group = GROUP_RE.match(stripped, pos)
            if group is None:
                break
            groups.append(group.group(1))
            pos = group.end()
        records.append(_parse_record(object_id, groups, image_width, image_height))
    if not records:
        raise ProviderResponseError("SAM output has no parseable records")
    return records


def _split_group(body: str, kind: str) -> dict[str, str]:
    prefix = f"{kind};"
    if not body.startswith(prefix):
        raise ProviderResponseError(f"SAM record has an unexpected group '{body[:20]}'")
    fields: dict[str, str] = {}
    for part in body[len(prefix) :].split(";"):
        if "=" not in part:
            raise ProviderResponseError(
                f"SAM record has a malformed field '{part[:20]}'"
            )
        key, _, value = part.partition("=")
        fields[key] = value
    return fields


def _parse_record(
    object_id: int, groups: list[str], image_width: int, image_height: int
) -> SegmentRecord:
    boxes = [body for body in groups if body == "box" or body.startswith("box;")]
    masks = [body for body in groups if body == "mask" or body.startswith("mask;")]
    if len(groups) != len(boxes) + len(masks):
        raise ProviderResponseError(f"SAM record {object_id} has an unknown group")
    if len(boxes) != 1 or len(masks) != 1:
        raise ProviderResponseError(
            f"SAM record {object_id} must hold exactly one box and one mask"
        )
    box_fields = _split_group(boxes[0], "box")
    x1 = _parse_int(box_fields.get("x1"), "box x1")
    y1 = _parse_int(box_fields.get("y1"), "box y1")
    x2 = _parse_int(box_fields.get("x2"), "box x2")
    y2 = _parse_int(box_fields.get("y2"), "box y2")
    w = _parse_int(box_fields.get("w"), "box width")
    h = _parse_int(box_fields.get("h"), "box height")
    if w != image_width or h != image_height:
        raise ProviderResponseError(
            f"SAM record {object_id} frame {w}x{h} drifts from the image"
        )
    if not (0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h):
        raise ProviderResponseError(
            f"SAM record {object_id} box is out of bounds (rejected, not clipped)"
        )
    confidence = _parse_confidence(box_fields.get("c"))

    mask_fields = _split_group(masks[0], "mask")
    if (
        _parse_int(mask_fields.get("x"), "mask x") != 0
        or _parse_int(mask_fields.get("y"), "mask y") != 0
    ):
        raise ProviderResponseError(f"SAM record {object_id} mask origin drifts")
    data = mask_fields.get("data")
    if not data:
        raise ProviderResponseError(f"SAM record {object_id} mask lacks data")
    height_s, comma1, rest = data.partition(",")
    width_s, comma2, payload = rest.partition(",")
    if not comma1 or not comma2 or not payload:
        raise ProviderResponseError(f"SAM record {object_id} mask data is malformed")
    mask_h = _parse_int(height_s, "mask height")
    mask_w = _parse_int(width_s, "mask width")
    if mask_h <= 0 or mask_w <= 0 or mask_h * mask_w > MAX_MASK_PIXELS:
        raise ProviderResponseError(f"SAM record {object_id} mask size is invalid")
    marker, encoded = payload[0], payload[1:]
    if marker == "~":
        encoding = "lossless"
    elif marker == "!":
        encoding = "one_bit"
    else:
        raise ProviderResponseError(f"SAM record {object_id} mask encoding is unknown")
    if not encoded:
        raise ProviderResponseError(f"SAM record {object_id} mask payload is empty")
    try:
        # The codec expects the marker-prefixed payload exactly as emitted.
        raster = decode_mask_to_raster(
            SegmentationMask(
                encoding=encoding, payload=payload, width=mask_w, height=mask_h
            )
        )
    except InvalidSegmentationMaskError as exc:
        raise ProviderResponseError(
            f"SAM record {object_id} mask does not decode: {exc}"
        ) from exc
    if len(raster) != mask_h * mask_w:
        raise ProviderResponseError(f"SAM record {object_id} raster size drifts")
    return SegmentRecord(
        object_id=object_id,
        box=(x1, y1, x2, y2),
        width=w,
        height=h,
        raster=raster,
        encoding=encoding,
        provider_confidence=confidence,
    )


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class MetaSamAdapter(ProviderAdapter):
    provider_name = "meta"
    model_id = SAM31_MODEL_ID
    api_base = META_API_BASE
    env_key_name = "META_API_KEY"
    timeout_s = 60.0
    retry_policy = RetryPolicy(max_retries=3)

    def analyze(
        self,
        request: SamRequest,
        *,
        client: httpx.Client | None = None,
        sleep=_sleep,
    ) -> SamSegmentationResult:
        """Run one SAM segmentation call with bounded retries."""
        self.require_configured()
        policy = self.retry_policy
        retries = 0
        last_error: ProviderError | None = None
        started = time.monotonic()
        owned = client if client is not None else httpx.Client(timeout=self.timeout_s)
        try:
            for attempt in range(policy.max_retries + 1):
                self.live_calls_attempted += 1
                try:
                    with owned.stream(
                        "POST",
                        f"{self.api_base}/responses",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json={**request.to_payload(), "model": self.model_id},
                    ) as response:
                        if response.status_code != 200:
                            last_error = self.map_http_error(
                                response.status_code,
                                response.read().decode("utf-8", errors="replace"),
                            )
                        else:
                            self.last_attempts = retries + 1
                            text = parse_sse_output_text(response.iter_lines())
                            records = parse_segmentation_records(
                                text, request.image_width, request.image_height
                            )
                            usage = self._record(request, retries, started, None)
                            return SamSegmentationResult(
                                records=tuple(records),
                                model=self.model_id,
                                usage=usage,
                            )
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    last_error = ProviderTimeoutError(
                        f"meta request failed: {exc.__class__.__name__}"
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
                owned.close()
        assert last_error is not None
        self.last_attempts = retries + 1
        self._record(request, retries, started, last_error)
        raise last_error

    @staticmethod
    def _retryable(error: ProviderError | None) -> bool:
        if isinstance(error, (ProviderTimeoutError, ProviderRateLimitError)):
            return True
        return error is not None and error.status is not None and error.status >= 500

    def _record(
        self,
        request: SamRequest,
        retries: int,
        started: float,
        error: ProviderError | None,
    ) -> UsageRecord:
        latency_ms = int((time.monotonic() - started) * 1000)
        record = UsageRecord(
            provider=self.provider_name,
            model_id=self.model_id,
            prompt_version="sam-token-grammar-v1",
            photo_ids=(request.photo.photo_id,),
            status="error" if error else "ok",
            latency_ms=latency_ms,
            retries=retries,
            estimated_cost_usd=None if error else META_PRICE_PER_IMAGE_USD,
            pricing_version=META_PRICING_VERSION,
            error=str(error) if error else None,
        )
        self.ledger.record(record)
        return record
