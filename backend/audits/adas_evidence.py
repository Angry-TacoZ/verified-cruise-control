"""Layer A/D: feature normalization + ADAS evidence interpretation.

Muse (Layer B) identifies physical controls; SAM (Layer C) localizes the
observed object; this module (Layer D) deterministically maps validated
object observations to supported vehicle capabilities using the
versioned reference catalog (``adas_catalog_v2.json`` by default).

Nothing here calls a model. Unknown controls, unknown manufacturers, and
ambiguous matches all resolve to documented unknowns — never to invented
equipment knowledge. Cross-manufacturer visual patterns (scope
``general_pattern``) may support a capability up to ``visually_supported``
but can never verify it; verification still requires authoritative
vehicle-specific evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import Verdict

CATALOG_PATH = Path(__file__).resolve().parent / "adas_catalog_v2.json"
CATALOG_V1_PATH = Path(__file__).resolve().parent / "adas_catalog_v1.json"

FEATURE_UNKNOWN = "unknown"

STRENGTH_RANK = {"diagnostic": 3, "supportive": 2, "ambiguous": 1, "non_diagnostic": 0}

# Ceiling ladder over SUPPORT STATES ONLY, strongest first: a ceiling
# caps how strong support may be, so the negative finding
# "contradicted" is not on the ladder and can never be a ceiling.
STATE_CEILING_RANK = {
    "not_visually_verifiable": 0,
    "insufficient_evidence": 1,
    "visually_supported": 2,
    "verified": 3,
}


def apply_verdict_ceiling(state: str, ceilings: list[str | None]) -> str:
    """Clamp a mapped evidence state to supporting entries' ceilings.

    Uses the highest ceiling among supporters; entries with no ceiling
    (None) are ignored, so a single capped supporter caps the verdict
    even when an uncapped supporter is present — the conservative
    direction. Off-ladder ceilings (including "contradicted") fail
    closed with ValueError. A state that is not on the ladder (e.g. a
    "contradicted" negative finding) is returned unchanged: the clamp
    never alters a negative finding, and the result is always on the
    ladder and never stronger than the input state.
    """
    if state not in STATE_CEILING_RANK:
        return state
    ranked = []
    for ceiling in ceilings:
        if ceiling is None:
            continue
        if ceiling not in STATE_CEILING_RANK:
            raise ValueError(f"bad verdict ceiling {ceiling!r}")
        ranked.append(STATE_CEILING_RANK[ceiling])
    if not ranked:
        return state
    cap = max(ranked)
    if STATE_CEILING_RANK[state] <= cap:
        return state
    return next(name for name, rank in STATE_CEILING_RANK.items() if rank == cap)


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
        "scope",
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
    scope: str
    manufacturer: str | None
    models: tuple[str, ...]
    model_years: tuple[int, ...]
    trim: str | None
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
    verdict_ceiling: str | None = None

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
    scope: str = ""
    verdict_ceiling: str | None = None


@dataclass(frozen=True)
class ControlItem:
    """One individually identifiable physical control in an observation.

    Index 0 is the top-level observed object; higher indexes are the
    validated nested ``controls`` entries in order.
    """

    index: int
    visible_object: str
    control_symbol: str = ""
    control_type: str = ""
    location: str = "unknown"
    dedicated: bool | None = None
    manufacturer_context: str = ""


MAX_ITEMS_PER_OBSERVATION = 5


def _item_key(item: ControlItem) -> tuple[str, str, str, str]:
    return (
        item.visible_object.strip().lower(),
        item.control_symbol.strip().lower(),
        item.control_type.strip().lower(),
        item.location.strip().lower(),
    )


def iter_control_items(extra: dict) -> list[ControlItem]:
    """Top-level control plus validated nested controls, deduplicated.

    Nested items carry their own symbol/type/location and inherit only
    the photo-level manufacturer context — dedication and placement are
    never copied across distinct physical controls. A nested item that
    merely restates the top-level control is skipped so one object is
    never double-counted, and the total is capped (top + validator's
    at-most-4 nested) so a malformed row cannot widen the evidence set.
    """
    context = str(extra.get("manufacturer_context", "") or "")
    top = ControlItem(
        index=0,
        visible_object=str(extra.get("visible_object", "") or ""),
        control_symbol=str(extra.get("control_symbol", "") or ""),
        control_type=str(extra.get("control_type", "") or ""),
        location=str(extra.get("location", "unknown") or "unknown"),
        dedicated=extra.get("dedicated"),
        manufacturer_context=context,
    )
    items = [top]
    seen = {_item_key(top)}
    nested = extra.get("controls", [])
    if not isinstance(nested, list):
        return items
    for position, raw in enumerate(nested, start=1):
        if len(items) >= MAX_ITEMS_PER_OBSERVATION:
            break
        if not isinstance(raw, dict):
            continue
        candidate = ControlItem(
            index=position,
            visible_object=str(raw.get("visible_object", "") or ""),
            control_symbol=str(raw.get("control_symbol", "") or ""),
            control_type=str(raw.get("control_type", "") or ""),
            location=str(raw.get("location", "unknown") or "unknown"),
            dedicated=None,
            manufacturer_context=context,
        )
        if not candidate.visible_object.strip():
            continue
        if _item_key(candidate) in seen:
            continue
        seen.add(_item_key(candidate))
        items.append(candidate)
    return items


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
        scope = item["scope"]
        if scope == "general":
            if (
                item["manufacturer"] is not None
                or item["models"]
                or item["model_years"]
            ):
                raise ValueError(
                    f"catalog entry {entry_id}: general entries must not "
                    "carry make/model/year applicability"
                )
        elif scope == "general_pattern":
            if (
                item["manufacturer"] is not None
                or item["models"]
                or item["model_years"]
            ):
                raise ValueError(
                    f"catalog entry {entry_id}: general-pattern entries must "
                    "not carry make/model/year applicability"
                )
            ceiling = item.get("verdict_ceiling")
            if ceiling != "visually_supported":
                raise ValueError(
                    f"catalog entry {entry_id}: general-pattern ceiling must "
                    f"be visually_supported, got {ceiling!r}"
                )
            if item["strength"] not in (
                "supportive",
                "ambiguous",
                "non_diagnostic",
            ):
                raise ValueError(
                    f"catalog entry {entry_id}: general-pattern strength "
                    f"above the cap is not allowed, got {item['strength']!r}"
                )
        elif scope == "manufacturer_specific":
            if not item["manufacturer"] or not item["models"]:
                raise ValueError(
                    f"catalog entry {entry_id}: manufacturer-specific "
                    "entries need make/model applicability"
                )
        else:
            raise ValueError(f"catalog entry {entry_id}: bad scope {scope!r}")
        ceiling = item.get("verdict_ceiling")
        if ceiling is not None and ceiling not in STATE_CEILING_RANK:
            raise ValueError(
                f"catalog entry {entry_id}: bad verdict ceiling {ceiling!r}"
            )
        trim = item.get("trim")
        if trim is not None and not isinstance(trim, str):
            raise ValueError(f"catalog entry {entry_id}: bad trim")
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
                scope=item["scope"],
                manufacturer=item["manufacturer"],
                models=tuple(item["models"]),
                model_years=tuple(item["model_years"]),
                trim=item.get("trim"),
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
                verdict_ceiling=item.get("verdict_ceiling"),
            )
        )
    return Catalog(version=version, feature_ids=feature_ids, entries=tuple(entries))


def _match_detail(entry: CatalogEntry, haystack: str) -> tuple[int, int, int]:
    """(keyword hits, phrase hits, matched chars) in observed-control text.

    Multi-word keywords match as phrases; single words match on word
    boundaries, so "cruise" never matches "cruiser".
    """
    score = 0
    phrases = 0
    chars = 0
    for keyword in entry.match_keywords:
        phrase = keyword.strip().lower()
        if not phrase:
            continue
        if " " in phrase:
            hit = phrase in haystack
        else:
            hit = re.search(rf"\b{re.escape(phrase)}\b", haystack) is not None
        if hit:
            score += 1
            chars += len(phrase)
            if " " in phrase:
                phrases += 1
    return score, phrases, chars


def match_entry(
    catalog: Catalog,
    *,
    visible_object: str,
    control_symbol: str = "",
    control_type: str = "",
    vehicle: dict | None = None,
    scopes: tuple[str, ...] | None = None,
    applicable_only: bool = False,
) -> CatalogEntry | None:
    """Find the catalog entry for an observed control, if exactly one fits.

    Naming-only entries never match physical controls. Candidates rank
    by identifier specificity (phrase hits, then matched characters)
    first — lexical fit outranks everything — then by
    manufacturer-specific scope (the documented entry for the vehicle
    wins exact-specificity ties over a general pattern), then by
    verified vehicle applicability, then by raw keyword count. So a
    documented multi-word control identifier beats a generic overlapping
    word, but an inapplicable specific entry can still outrank an
    applicable one lexically; that is why :func:`interpret_control`
    phase 1 passes ``applicable_only=True`` (a filter, applied before
    ranking) instead of relying on the sort key. Full-key ties and
    zero-score controls resolve to ``None`` (documented unknown) rather
    than a guessed association. Only observed-control text is matched;
    claim wording never enters selection.

    ``scopes`` optionally restricts candidates to the given entry
    scopes (used by the two-phase specific-then-general lookup in
    :func:`interpret_control`); ``None`` matches any scope.
    ``applicable_only`` drops candidates that are not applicable to the
    vehicle before ranking; default ``False`` preserves the legacy
    rank-then-check behavior byte-identically.
    """
    haystack = f"{visible_object} {control_symbol} {control_type}".lower()
    scored = []
    for entry in catalog.entries:
        if entry.is_naming_only:
            continue
        if scopes is not None and entry.scope not in scopes:
            continue
        score, phrases, chars = _match_detail(entry, haystack)
        if score <= 0:
            continue
        eligible, _ = entry_applicable(entry, vehicle)
        if applicable_only and not eligible:
            continue
        scored.append(
            (
                entry.scope == "manufacturer_specific",
                (phrases, chars),
                eligible,
                score,
                entry,
            )
        )
    if not scored:
        return None
    scored.sort(
        key=lambda candidate: (candidate[1], candidate[0], candidate[2], candidate[3]),
        reverse=True,
    )
    if len(scored) > 1 and scored[0][:4] == scored[1][:4]:
        return None
    return scored[0][4]


def _downgrade(strength: str) -> str:
    order = ("non_diagnostic", "ambiguous", "supportive", "diagnostic")
    return order[max(0, order.index(strength) - 1)]


def _norm_name(text: object) -> str:
    return str(text or "").strip().casefold()


def _model_match(entry_model: str, vehicle_model: str) -> bool:
    """Entry model vs audit vehicle model (case-insensitive containment).

    "Forte" matches "Forte5" (same family, shared documentation); "Forte"
    never matches "K5".
    """
    a, b = _norm_name(entry_model), _norm_name(vehicle_model)
    return bool(a and b) and (a == b or a in b or b in a)


def _vehicle_label(vehicle: dict) -> str:
    label = " ".join(
        part
        for part in (
            str(vehicle.get("make") or "").strip(),
            str(vehicle.get("model") or "").strip(),
        )
        if part
    )
    year = vehicle.get("year")
    if year not in (None, ""):
        label = f"{label} {year}".strip()
    return label or "unknown vehicle"


def _entry_span(entry: CatalogEntry) -> str:
    bits = []
    if entry.manufacturer:
        bits.append(entry.manufacturer)
    if entry.models:
        bits.append("/".join(entry.models))
    if entry.model_years:
        years = ", ".join(str(year) for year in sorted(entry.model_years))
        bits.append(f"({years})")
    return " ".join(bits)


def entry_applicable(entry: CatalogEntry, vehicle: dict | None) -> tuple[bool, str]:
    """Whether a matched entry may support a claim for this audit vehicle.

    General and general-pattern entries apply to any vehicle (including
    unknown ones); general patterns are capped at visually_supported by
    the interpretation layer. Manufacturer-specific entries require a
    documented make/model/year match against the audit's trusted listing
    metadata — never against the model's visual guess. Missing or
    uncertain identity rejects the entry rather than merely weakening it.
    Returns (eligible, reason).
    """
    if entry.scope == "general":
        return True, "general mapping"
    if entry.scope == "general_pattern":
        return True, (
            "general-pattern mapping applies to any vehicle; "
            "evidence capped at visually_supported"
        )
    if not vehicle:
        return False, (
            "audit vehicle identity is unknown; entry verified for "
            f"{_entry_span(entry)} only"
        )
    make = _norm_name(vehicle.get("make"))
    model = _norm_name(vehicle.get("model"))
    if not make or not model:
        return False, (
            "audit vehicle identity is incomplete; entry verified for "
            f"{_entry_span(entry)} only"
        )
    label = _vehicle_label(vehicle)
    span = _entry_span(entry)
    if _norm_name(entry.manufacturer) != make:
        return False, (f"entry is not verified for {label}; verified for {span} only")
    if entry.models and not any(
        _model_match(candidate, model) for candidate in entry.models
    ):
        return False, (f"entry is not verified for {label}; verified for {span} only")
    if entry.model_years:
        years = ", ".join(str(year) for year in sorted(entry.model_years))
        raw_year = vehicle.get("year")
        if raw_year in (None, ""):
            return False, (f"audit vehicle model year is unknown; entry covers {years}")
        try:
            year = int(str(raw_year).strip())
        except ValueError:
            return False, (
                f"audit vehicle model year {raw_year!r} is uncertain; "
                f"entry covers {years}"
            )
        if year not in entry.model_years:
            return False, (
                f"entry covers model years {years}; audit vehicle is a {year}"
            )
    if entry.trim:
        trim = str(vehicle.get("trim") or "").strip()
        if not trim:
            return False, (
                f"audit vehicle trim is unknown; entry verified for trim {entry.trim}"
            )
        if trim.casefold() != entry.trim.strip().casefold():
            return False, (
                f"entry verified for trim {entry.trim}; audit vehicle trim {trim}"
            )
    return True, "applicable"


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
    vehicle: dict | None = None,
) -> Interpretation:
    """Map one observed control to what it implies about the claim.

    A documented catalog entry plus a matching documented location yields
    the entry's strength; an undocumented location downgrades one level
    (location is context, but documented placement is part of the
    evidence), as does a model-observed shared/menu control matching a
    dedicated entry. The catalog wins disagreements about menu-ness: a
    documented menu never upgrades. Lookup is two-phase: an applicable
    manufacturer-specific entry always wins over a general pattern
    (phase 1 filters inapplicable entries before ranking, so a
    lexically stronger but inapplicable entry cannot hide an applicable
    one), while no applicable specific entry falls through to the
    general scopes (general or general_pattern) instead of closing the
    case as unknown. A general-pattern match is structurally capped at
    supportive strength and labeled as cross-manufacturer evidence for
    reviewer confirmation.
    Controls with no entry, tied entries, menu/non-diagnostic entries,
    and feature mismatches never support the claim. The model's visual
    manufacturer guess never overrides listing metadata for
    applicability, but a conflict is recorded explicitly.
    """
    # Two-phase precedence: an applicable manufacturer-specific entry
    # always wins over a general pattern (phase 1 filters out
    # inapplicable entries before ranking, so a lexically stronger but
    # inapplicable entry can no longer hide an applicable one). An
    # applicable match is used directly; otherwise the general scopes
    # apply instead of closing the case as unknown. A documented
    # menu/non-diagnostic specific entry is used as matched and is never
    # upgraded by a general pattern (it stays non-supporting below).
    observed = control_symbol or visible_object
    entry = match_entry(
        catalog,
        visible_object=visible_object,
        control_symbol=control_symbol,
        control_type=control_type,
        vehicle=vehicle,
        scopes=("manufacturer_specific",),
        applicable_only=True,
    )
    if entry is None:
        entry = match_entry(
            catalog,
            visible_object=visible_object,
            control_symbol=control_symbol,
            control_type=control_type,
            vehicle=vehicle,
            scopes=("general", "general_pattern"),
        )
    if entry is None:
        # No applicable entry of any scope: name the top unfiltered
        # specific match (if any) as the documented-unknown referent so
        # the explanation still says what was seen and why it cannot be
        # used. The referent never supports the claim.
        dropped = match_entry(
            catalog,
            visible_object=visible_object,
            control_symbol=control_symbol,
            control_type=control_type,
            vehicle=vehicle,
            scopes=("manufacturer_specific",),
        )
        if dropped is not None:
            _, applicability = entry_applicable(dropped, vehicle)
            return Interpretation(
                entry_id=dropped.id,
                feature=dropped.feature,
                strength="unknown",
                supports_claim=False,
                cannot_infer=dropped.cannot_infer,
                explanation=(
                    f"observed {observed!r} matches catalog entry {dropped.id}, "
                    f"but {applicability}"
                ),
                reference="; ".join(
                    f"{source.get('doc', '')} [{dropped.certainty}]".strip()
                    for source in dropped.sources
                ),
                scope=dropped.scope,
                verdict_ceiling=dropped.verdict_ceiling,
            )
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
    if entry.scope == "general_pattern":
        # Structural verdict ceiling (defense in depth behind the loader
        # rejection): a general cross-manufacturer pattern can never be
        # stronger than supportive, so photo evidence built on it caps at
        # visually_supported however the entry was constructed.
        if STRENGTH_RANK.get(strength, -1) > STRENGTH_RANK["supportive"]:
            strength = "supportive"
        notes.append(
            "general cross-manufacturer pattern, not vehicle-specific "
            "evidence; support caps at visually_supported and the "
            "reviewer should confirm the control against the actual vehicle"
        )
    context = (manufacturer_context or "").strip()
    listing_make = _norm_name((vehicle or {}).get("make"))
    listing_model = _norm_name((vehicle or {}).get("model"))
    lowered = context.lower()
    if (
        context
        and listing_make
        and listing_make not in lowered
        and listing_model not in lowered
    ):
        notes.append(
            f"model context ({context}) does not confirm audit vehicle "
            f"({_vehicle_label(vehicle or {})}); listing metadata governs "
            "applicability"
        )
    location_note = "; ".join(notes)
    reference = "; ".join(
        f"{source.get('doc', '')} [{entry.certainty}]".strip()
        for source in entry.sources
    )
    # ``entry`` is eligible by construction here: a specific entry is
    # only used after an applicability check, and the general scopes
    # always apply. Inapplicable specific matches are handled above
    # (fall through to the general scopes, else documented unknown).
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
            scope=entry.scope,
            verdict_ceiling=entry.verdict_ceiling,
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
            scope=entry.scope,
            verdict_ceiling=entry.verdict_ceiling,
        )
    supports = strength in ("diagnostic", "supportive")
    explanation = (
        f"observed {observed!r} ({entry.control_description}) is "
        f"{strength} evidence for {claimed_feature} per "
        f"{entry.official_feature_name}"
    )
    if supports and entry.scope == "general_pattern":
        # A.6 truthful labeling: the finding-level summary (reconcile)
        # quotes this explanation, so the general-pattern nature travels
        # with the verdict and prompts reviewer confirmation.
        explanation += (
            " This support is a general cross-manufacturer pattern, not "
            "vehicle-specific evidence; the reviewer should confirm the "
            "control against the actual vehicle."
        )
    return Interpretation(
        entry_id=entry.id,
        feature=entry.feature,
        strength=strength,
        supports_claim=supports,
        cannot_infer=entry.cannot_infer,
        explanation=explanation,
        reference=reference,
        location_note=location_note,
        scope=entry.scope,
        verdict_ceiling=entry.verdict_ceiling,
    )


def _obs_id(value: object) -> str | None:
    return str(value) if value is not None else None


def explain_claim(
    catalog: Catalog,
    observations: list,
    claim_text: str,
    vehicle: dict | None = None,
) -> dict | None:
    """Best Layer D interpretation for a claim's keyed observations.

    Every control item in every PRESENT keyed observation is interpreted
    independently; all interpretations are retained (with observation,
    photo, and control provenance) while the primary explanation is the
    strongest interpretation that actually supports the verdict — a
    supported finding is never justified by a non-supporting control.
    With no supporters, the primary explanation states the strongest
    relevant limitation. Returns a JSON-serializable dict for the
    finding evidence block, or ``None`` when no observation carries
    control identification.
    """
    feature = normalize_feature(claim_text)
    candidates: list[tuple[Interpretation, object, int, ControlItem]] = []
    for obs in observations:
        if obs.verdict != Verdict.PRESENT:
            continue
        extra = obs.extra or {}
        if "visible_object" not in extra:
            continue
        for item in iter_control_items(extra):
            candidates.append(
                (
                    interpret_control(
                        catalog,
                        feature,
                        visible_object=item.visible_object,
                        control_symbol=item.control_symbol,
                        control_type=item.control_type,
                        location=item.location,
                        dedicated=item.dedicated,
                        manufacturer_context=item.manufacturer_context,
                        vehicle=vehicle,
                    ),
                    obs,
                    item.index,
                    item,
                )
            )
    if not candidates:
        return None
    supporters = [c for c in candidates if c[0].supports_claim]
    best, best_obs, best_index, _ = max(
        supporters or candidates, key=lambda c: STRENGTH_RANK.get(c[0].strength, -1)
    )
    return {
        "catalog_version": catalog.version,
        "claimed_feature": feature,
        "entry_id": best.entry_id,
        "feature": best.feature,
        "strength": best.strength,
        "supports_claim": best.supports_claim,
        "scope": best.scope,
        "cannot_infer": list(best.cannot_infer),
        "explanation": best.explanation,
        "reference": best.reference,
        "location_note": best.location_note,
        "observation_id": _obs_id(getattr(best_obs, "id", None)),
        "photo_id": _obs_id(getattr(best_obs, "photo_id", None)),
        "control_index": best_index,
        "interpretations": [
            {
                "observation_id": _obs_id(getattr(obs, "id", None)),
                "photo_id": _obs_id(getattr(obs, "photo_id", None)),
                "control_index": index,
                "observed_object": item.visible_object,
                "entry_id": interp.entry_id,
                "feature": interp.feature,
                "strength": interp.strength,
                "supports_claim": interp.supports_claim,
                "scope": interp.scope,
                "explanation": interp.explanation,
                "reference": interp.reference,
            }
            for interp, obs, index, item in candidates
        ],
    }
