"""Layer A/D: feature normalization + ADAS evidence interpretation.

Muse (Layer B) identifies physical controls; SAM (Layer C) localizes the
observed object; this module (Layer D) deterministically maps validated
object observations to supported vehicle capabilities using the
versioned reference catalog (``adas_catalog_v1.json``).

Nothing here calls a model. Unknown controls, unknown manufacturers, and
ambiguous matches all resolve to documented unknowns — never to invented
equipment knowledge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import Verdict

CATALOG_PATH = Path(__file__).resolve().parent / "adas_catalog_v1.json"
CATALOG_VERSION = "2026-11-01/v1"

FEATURE_UNKNOWN = "unknown"

STRENGTH_RANK = {"diagnostic": 3, "supportive": 2, "ambiguous": 1, "non_diagnostic": 0}
ENTRY_CERTAINTIES = frozenset({"confirmed", "provisional", "definitional"})

LOCATION_CATEGORIES = frozenset(
    {
        "steering_wheel",
        "left_dashboard",
        "stalk",
        "instrument_cluster",
        "infotainment",
        "center_console",
        "exterior_sensor",
        "windshield_camera",
        "unknown",
    }
)

# Most-specific first: centering language beats keeping, keeping beats
# departure, and bare "lane assist" settles at the keeping tier (VW
# convention, catalog entry vw-lane-assist-naming) — centering always
# needs explicit centering/following language.
_FEATURE_RULES: tuple[tuple[str, str], ...] = (
    ("hands_free_driving", r"hands[\s-]?free"),
    ("travel_assist", r"travel\s+assist"),
    ("highway_assist", r"highway\s+(driving\s+)?assist|highway\s+driving"),
    (
        "lane_centering_assist",
        (
            r"lane\s+(centering|center|centreing|centre|following|follow)|"
            r"\blfa\b|center(ing|ed)?\s+in\s+lane|keep(s)?\s+.*\s+centered"
        ),
    ),
    (
        "lane_keeping_assist",
        (
            r"lane\s+(keeping|keep|assist|assistance)|\blka\b|"
            r"keep(s)?\s+.*\s+in\s+(its|the)\s+lane"
        ),
    ),
    (
        "lane_departure_warning",
        r"lane\s+departure|departure\s+warning|\bldw\b|warn(s)?\s+.*\s+lane\s+depart",
    ),
    (
        "adaptive_cruise_control",
        (
            r"adaptive\s+cruise|smart\s+cruise|\bacc\b|\bscc\b|"
            r"dynamic\s+radar\s+cruise|distronic|pro?pilot"
        ),
    ),
    ("cruise_control", r"cruise"),
)

_REQUIRED_ENTRY_KEYS = frozenset(
    {
        "id",
        "manufacturer",
        "models",
        "model_years",
        "official_feature_name",
        "feature",
        "control_category",
        "control_description",
        "symbol",
        "locations",
        "dedicated",
        "strength",
        "match_keywords",
        "distinctions",
        "cannot_infer",
        "sources",
        "certainty",
        "certainty_note",
    }
)


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    manufacturer: str | None
    models: tuple[str, ...]
    model_years: tuple[int, ...]
    official_feature_name: str
    feature: str | None
    control_category: str
    control_description: str
    symbol: str
    locations: tuple[str, ...]
    dedicated: bool
    strength: str
    match_keywords: tuple[str, ...]
    distinctions: str
    cannot_infer: tuple[str, ...]
    sources: tuple[dict, ...]
    certainty: str
    certainty_note: str

    @property
    def is_naming_only(self) -> bool:
        return self.control_category == "naming"


@dataclass(frozen=True)
class Catalog:
    version: str
    feature_ids: tuple[str, ...]
    entries: tuple[CatalogEntry, ...]

    def by_id(self, entry_id: str) -> CatalogEntry | None:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None


@dataclass(frozen=True)
class Interpretation:
    """What one observed control implies about the claimed capability."""

    entry_id: str | None
    feature: str | None
    strength: str
    supports_claim: bool
    cannot_infer: tuple[str, ...] = ()
    explanation: str = ""
    reference: str = ""
    location_note: str = ""


def normalize_feature(claim_text: str) -> str:
    """Map advertised wording to a normalized feature id (Layer A).

    Deterministic keyword rules; anything unrecognized is ``unknown``.
    Lane tiers never collapse upward: bare "lane assist" lands at the
    keeping tier, and centering/highway/hands-free need explicit words.
    """
    text = (claim_text or "").lower()
    for feature, pattern in _FEATURE_RULES:
        if re.search(pattern, text):
            return feature
    return FEATURE_UNKNOWN


def load_catalog(path: Path | None = None) -> Catalog:
    """Load and strictly validate the reference catalog.

    Every entry must carry the full schema including source provenance;
    anything malformed raises ``ValueError`` with the entry id.
    """
    raw = json.loads((path or CATALOG_PATH).read_text(encoding="utf-8"))
    version = raw.get("catalog_version", "")
    feature_ids = tuple(raw.get("feature_ids", []))
    entries = []
    for item in raw.get("entries", []):
        entry_id = item.get("id", "<missing id>")
        missing = _REQUIRED_ENTRY_KEYS - set(item)
        if missing:
            raise ValueError(f"catalog entry {entry_id}: missing {sorted(missing)}")
        if item["strength"] not in STRENGTH_RANK:
            raise ValueError(f"catalog entry {entry_id}: bad strength")
        if item["certainty"] not in ENTRY_CERTAINTIES:
            raise ValueError(f"catalog entry {entry_id}: bad certainty")
        feature = item["feature"]
        if feature is not None and feature not in feature_ids:
            raise ValueError(f"catalog entry {entry_id}: bad feature id")
        for other in item["cannot_infer"]:
            if other not in feature_ids:
                raise ValueError(f"catalog entry {entry_id}: bad cannot_infer id")
        for location in item["locations"]:
            if location not in LOCATION_CATEGORIES:
                raise ValueError(f"catalog entry {entry_id}: bad location")
        if not item["sources"]:
            raise ValueError(f"catalog entry {entry_id}: no sources")
        entries.append(
            CatalogEntry(
                id=item["id"],
                manufacturer=item["manufacturer"],
                models=tuple(item["models"]),
                model_years=tuple(item["model_years"]),
                official_feature_name=item["official_feature_name"],
                feature=item["feature"],
                control_category=item["control_category"],
                control_description=item["control_description"],
                symbol=item["symbol"],
                locations=tuple(item["locations"]),
                dedicated=bool(item["dedicated"]),
                strength=item["strength"],
                match_keywords=tuple(item["match_keywords"]),
                distinctions=item["distinctions"],
                cannot_infer=tuple(item["cannot_infer"]),
                sources=tuple(item["sources"]),
                certainty=item["certainty"],
                certainty_note=item["certainty_note"],
            )
        )
    return Catalog(version=version, feature_ids=feature_ids, entries=tuple(entries))


def _match_score(entry: CatalogEntry, haystack: str) -> int:
    """Count entry keywords present in the observed-control text.

    Multi-word keywords match as phrases; single words match on word
    boundaries, so "cruise" never matches "cruiser".
    """
    score = 0
    for keyword in entry.match_keywords:
        phrase = keyword.strip().lower()
        if not phrase:
            continue
        if " " in phrase:
            if phrase in haystack:
                score += 1
        elif re.search(rf"\b{re.escape(phrase)}\b", haystack):
            score += 1
    return score


def match_entry(
    catalog: Catalog,
    *,
    visible_object: str,
    control_symbol: str = "",
    control_type: str = "",
) -> CatalogEntry | None:
    """Find the catalog entry for an observed control, if exactly one fits.

    Naming-only entries never match physical controls. Score ties and
    zero-score controls resolve to ``None`` (documented unknown) rather
    than a guessed association.
    """
    haystack = f"{visible_object} {control_symbol} {control_type}".lower()
    best: CatalogEntry | None = None
    best_score = 0
    tied = False
    for entry in catalog.entries:
        if entry.is_naming_only:
            continue
        score = _match_score(entry, haystack)
        if score <= 0:
            continue
        if score > best_score:
            best, best_score, tied = entry, score, False
        elif score == best_score:
            tied = True
    if best is None or tied:
        return None
    return best


def _downgrade(strength: str) -> str:
    order = ("non_diagnostic", "ambiguous", "supportive", "diagnostic")
    return order[max(0, order.index(strength) - 1)]


def interpret_control(
    catalog: Catalog,
    claimed_feature: str,
    *,
    visible_object: str,
    control_symbol: str = "",
    control_type: str = "",
    location: str = "unknown",
    dedicated: bool | None = None,
    manufacturer_context: str = "",
) -> Interpretation:
    """Map one observed control to what it implies about the claim.

    A documented catalog entry plus a matching documented location yields
    the entry's strength; an undocumented location downgrades one level
    (location is context, but documented placement is part of the
    evidence), as does a model-observed shared/menu control matching a
    dedicated entry. The catalog wins disagreements about menu-ness: a
    documented menu never upgrades. Controls with no entry, tied entries,
    menu/non-diagnostic entries, and feature mismatches never support
    the claim.
    """
    entry = match_entry(
        catalog,
        visible_object=visible_object,
        control_symbol=control_symbol,
        control_type=control_type,
    )
    observed = control_symbol or visible_object
    if entry is None:
        return Interpretation(
            entry_id=None,
            feature=None,
            strength="unknown",
            supports_claim=False,
            explanation=(
                f"observed {observed!r} matches no cataloged control; "
                "capability cannot be established from it"
            ),
        )
    strength = entry.strength
    notes = []
    normalized_location = location if location in LOCATION_CATEGORIES else "unknown"
    if (
        entry.locations
        and normalized_location != "unknown"
        and normalized_location not in entry.locations
    ):
        strength = _downgrade(strength)
        notes.append(
            f"observed at {normalized_location}, documented at "
            f"{'/'.join(entry.locations)}; strength downgraded to {strength}"
        )
    if dedicated is False and entry.dedicated:
        strength = _downgrade(strength)
        notes.append(
            "model reports a shared/menu control against a dedicated "
            f"catalog entry; strength downgraded to {strength}"
        )
    elif dedicated is True and not entry.dedicated and entry.feature is None:
        notes.append(
            "model reports a dedicated control but the catalog documents "
            "a shared menu; catalog wins"
        )
    context = (manufacturer_context or "").strip().lower()
    if context and entry.manufacturer:
        maker = entry.manufacturer.lower()
        models = [model.lower() for model in entry.models]
        if maker not in context and not any(model in context for model in models):
            strength = _downgrade(strength)
            notes.append(
                f"context names a different vehicle ({manufacturer_context}); "
                f"entry verified for {entry.manufacturer} "
                f"{'/'.join(entry.models)}; strength downgraded to {strength}"
            )
    location_note = "; ".join(notes)
    reference = "; ".join(
        f"{source.get('doc', '')} [{entry.certainty}]".strip()
        for source in entry.sources
    )
    if entry.feature is None:
        return Interpretation(
            entry_id=entry.id,
            feature=None,
            strength="non_diagnostic",
            supports_claim=False,
            cannot_infer=entry.cannot_infer,
            explanation=(
                f"observed {observed!r} is a general assistance control "
                f"({entry.control_description}); it does not establish any "
                "specific capability"
            ),
            reference=reference,
            location_note=location_note,
        )
    if entry.feature != claimed_feature:
        return Interpretation(
            entry_id=entry.id,
            feature=entry.feature,
            strength=strength,
            supports_claim=False,
            cannot_infer=entry.cannot_infer,
            explanation=(
                f"observed {observed!r} maps to {entry.feature} "
                f"({entry.official_feature_name}), not to the claimed "
                f"{claimed_feature}; {entry.distinctions}"
            ),
            reference=reference,
            location_note=location_note,
        )
    supports = strength in ("diagnostic", "supportive")
    return Interpretation(
        entry_id=entry.id,
        feature=entry.feature,
        strength=strength,
        supports_claim=supports,
        cannot_infer=entry.cannot_infer,
        explanation=(
            f"observed {observed!r} ({entry.control_description}) is "
            f"{strength} evidence for {claimed_feature} per "
            f"{entry.official_feature_name}"
        ),
        reference=reference,
        location_note=location_note,
    )


def explain_claim(catalog: Catalog, observations: list, claim_text: str) -> dict | None:
    """Best Layer D interpretation for a claim's keyed observations.

    Returns a JSON-serializable dict for the finding evidence block, or
    ``None`` when no observation carries control identification.
    """
    feature = normalize_feature(claim_text)
    best: Interpretation | None = None
    for obs in observations:
        if obs.verdict != Verdict.PRESENT:
            continue
        extra = obs.extra or {}
        if "visible_object" not in extra:
            continue
        candidate = interpret_control(
            catalog,
            feature,
            visible_object=str(extra.get("visible_object", "")),
            control_symbol=str(extra.get("control_symbol", "") or ""),
            control_type=str(extra.get("control_type", "") or ""),
            location=str(extra.get("location", "unknown") or "unknown"),
            dedicated=extra.get("dedicated"),
            manufacturer_context=str(extra.get("manufacturer_context", "") or ""),
        )
        if best is None or STRENGTH_RANK.get(
            candidate.strength, -1
        ) > STRENGTH_RANK.get(best.strength, -1):
            best = candidate
    if best is None:
        return None
    return {
        "catalog_version": catalog.version,
        "claimed_feature": feature,
        "entry_id": best.entry_id,
        "feature": best.feature,
        "strength": best.strength,
        "supports_claim": best.supports_claim,
        "cannot_infer": list(best.cannot_infer),
        "explanation": best.explanation,
        "reference": best.reference,
        "location_note": best.location_note,
    }
