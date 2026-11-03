"""RSA general ADAS knowledge reference (PR #3, Milestone 1).

Strictly generic, source-cited automotive knowledge extracted from the
Road Safety Authority ADAS guide (September 2026). This is NOT
manufacturer equipment data: every entry has scope "general", and the
loader rejects anything else. Vehicle-specific conclusions stay in the
manufacturer-aware catalog (``adas_evidence``), which this module never
touches.

The ``guidance_for`` accessor produces short, feature-scoped snippets
for the optional reference-assisted recognition mode (Milestone 2).
Snippets describe what to distinguish visually; they never assert
that equipment is present.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

REFERENCE_PATH = Path(__file__).resolve().parent / "rsa_reference.json"
REFERENCE_VERSION = "2026-09-rsa-v1"

_ASSISTANCE_MODES = frozenset({"warn", "intervene", "continuous", "inform"})

_REQUIRED_ENTRY_KEYS = frozenset(
    {
        "id",
        "names",
        "definition",
        "assistance_mode",
        "symbols",
        "indicator_location",
        "confusable_with",
        "visual_support",
        "cannot_prove",
        "sources",
        "scope",
        "limitations",
    }
)

_REQUIRED_SOURCE_KEYS = frozenset({"section", "page"})


@dataclass(frozen=True)
class RsaSymbol:
    description: str
    location_hint: str
    page: int
    limitation: str


@dataclass(frozen=True)
class RsaConcept:
    id: str
    names: tuple[str, ...]
    definition: str
    assistance_mode: str
    symbols: tuple[RsaSymbol, ...]
    indicator_location: str
    confusable_with: tuple[str, ...]
    visual_support: str
    cannot_prove: str
    sources: tuple[dict, ...]
    scope: str
    limitations: str


@dataclass(frozen=True)
class RsaReference:
    version: str
    source_document: str
    market_scope: str
    entries: tuple[RsaConcept, ...] = field(default_factory=tuple)

    def by_id(self, concept_id: str) -> RsaConcept | None:
        for entry in self.entries:
            if entry.id == concept_id:
                return entry
        return None


def load_reference(path: Path | None = None) -> RsaReference:
    """Load and strictly validate the RSA general-knowledge reference.

    Every concept must carry the full schema including source section
    and page; scope must be "general" (manufacturer-specific mappings
    belong in the catalog, never here). Anything malformed raises
    ``ValueError`` with the concept id.
    """
    raw = json.loads((path or REFERENCE_PATH).read_text(encoding="utf-8"))
    version = raw.get("reference_version", "")
    entries = []
    for item in raw.get("entries", []):
        concept_id = item.get("id", "<missing id>")
        missing = _REQUIRED_ENTRY_KEYS - set(item)
        if missing:
            raise ValueError(f"RSA concept {concept_id}: missing {sorted(missing)}")
        if item["scope"] != "general":
            raise ValueError(
                f"RSA concept {concept_id}: scope must be 'general', "
                "manufacturer mappings belong in the catalog"
            )
        if item["assistance_mode"] not in _ASSISTANCE_MODES:
            raise ValueError(
                f"RSA concept {concept_id}: bad assistance_mode "
                f"{item['assistance_mode']!r}"
            )
        if not item["sources"]:
            raise ValueError(f"RSA concept {concept_id}: needs source provenance")
        for source in item["sources"]:
            if _REQUIRED_SOURCE_KEYS - set(source):
                raise ValueError(
                    f"RSA concept {concept_id}: source needs section and page"
                )
        for symbol in item["symbols"]:
            for key in ("description", "location_hint", "page", "limitation"):
                if key not in symbol:
                    raise ValueError(f"RSA concept {concept_id}: symbol missing {key}")
        entries.append(
            RsaConcept(
                id=item["id"],
                names=tuple(item["names"]),
                definition=item["definition"],
                assistance_mode=item["assistance_mode"],
                symbols=tuple(RsaSymbol(**s) for s in item["symbols"]),
                indicator_location=item["indicator_location"],
                confusable_with=tuple(item["confusable_with"]),
                visual_support=item["visual_support"],
                cannot_prove=item["cannot_prove"],
                sources=tuple(item["sources"]),
                scope=item["scope"],
                limitations=item["limitations"],
            )
        )
    return RsaReference(
        version=version,
        source_document=raw.get("source_document", ""),
        market_scope=raw.get("market_scope", ""),
        entries=tuple(entries),
    )


@lru_cache(maxsize=1)
def get_reference() -> RsaReference:
    return load_reference()


def guidance_for(feature_id: str, max_chars: int = 600) -> str:
    """Short feature-scoped recognition guidance for reference-assisted mode.

    Describes what visually distinguishes the capability and what must
    not be inferred; never asserts equipment presence. Returns "" for
    unknown features so callers fall back to baseline prompts.
    """
    entry = get_reference().by_id(feature_id)
    if entry is None:
        return ""
    confusable = (
        f" Do not confuse with: {', '.join(entry.confusable_with)}."
        if entry.confusable_with
        else ""
    )
    text = (
        f"Report only visible evidence; mark symbols unreadable when unclear. "
        f"{entry.definition}{confusable} "
        f"Visual support: {entry.visual_support} "
        f"Do not infer: {entry.cannot_prove}"
    )
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text
