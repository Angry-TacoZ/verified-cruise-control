# ADAS reference catalog

Versioned, manufacturer-aware evidence mapping for the audit engine
(Layer D). This document covers the schema, the entries actually
shipped, what remains unsupported, and how to extend the catalog.

## The four layers

**Advertised Claim → Muse Visual Observation → Manufacturer-Aware
Control Identification → SAM Object Localization → Feature Evidence
Mapping → Deterministic Reconciliation → Reviewable Finding**

| Layer | Owner | Responsibility |
|---|---|---|
| A — Advertised capability | Normalizer (`normalize_feature`) | Preserve the seller's exact wording; map to a normalized feature id (`adaptive_cruise_control`, `lane_keeping_assist`, …) or `unknown`. Tiers never collapse upward. |
| B — Observed object | Muse (prompt m2) | Identify physical controls: exact symbol, type, location, dedicated-vs-menu, manufacturer context. May report unreadable/unrecognized; never guesses. |
| C — Localization | SAM 3.1 | Localize the physical object Muse named (`sam_target`, `target_source: observed_object`). Legacy rows fall back to claim text; unsanitizable targets record UNCLEAR without spending. |
| D — Interpretation | `adas_evidence` + this catalog | Deterministically map the observed control to supported capabilities with source provenance. No model calls, no invented knowledge. |

Final verdicts stay in the five approved states; the interpretation
(strength, reference, cannot-infer list) travels separately in the
finding summary and `finding.evidence.interpretation`.

## Matching rules (deterministic)

1. Keyword/phrase match of the observed control text against entries
   (physical controls only; naming entries never match objects).
2. Score ties and zero-score controls → documented unknown, never a guess.
3. Observed location outside the entry's documented locations →
   downgrade one strength level.
4. Model-observed shared/menu control against a dedicated entry →
   downgrade one level. Documented menus never upgrade (catalog wins).
5. Manufacturer context naming a different vehicle than the entry's
   verified applicability → downgrade one level.
6. Feature mismatch (entry maps to X, claim is Y) → does not support,
   with the distinction spelled out; `cannot_infer` lists what the
   evidence cannot establish.

Strengths: `diagnostic` / `supportive` (both can support a claim) /
`ambiguous` / `non_diagnostic` / `unknown` (none support).

## Certainty levels (reference applicability, not probabilities)

- `confirmed`: official manufacturer document directly verified.
- `provisional`: manual mirror, excerpt, or third-party hosting; the
  document is official but this project has not fetched the original.
- `definitional`: true by control semantics (e.g. set/resume-only
  buttons cannot be ACC-specific), not by manual lookup.

## Shipped entries (catalog 2026-11-01/v1)

| Entry | Vehicle | Control | Maps to | Strength |
|---|---|---|---|---|
| `kia-forte-2021-scc-distance` | Kia Forte 2021–2022 | Vehicle Distance steering-wheel button (SCC) | adaptive_cruise_control | diagnostic |
| `kia-forte-2019-2024-lka-button` | Kia Forte 2019–2024 | LKA button, instrument panel lower-left + cluster indicator | lane_keeping_assist | diagnostic |
| `vw-jetta-gli-assist-stalk` | VW Jetta/GLI 2021–2023 | Stalk-end assistance button + infotainment menu (opener) | none (menu) | non_diagnostic |
| `generic-cruise-set-res` | any | ON/SET/RES buttons, no distance function | cruise_control | non_diagnostic |
| `kia-lfa-steering-buttons` | Kia K5/Seltos/Forte 2021–2024 | Lane Following Assist wheel controls | lane_centering_assist | supportive |
| `vw-lane-assist-naming` | VW Jetta/GLI/Golf 2021–2026 | naming only (no control) | lane_keeping_assist | supportive |

Sources (doc identity, URL actually consulted, section, access date) are
recorded per entry in `backend/audits/adas_catalog_v1.json`. No manual
illustrations are reproduced; visual fixtures are generated schematics
under `fixtures/adas/`.

## Explicitly unsupported / unverified

- Every other manufacturer (Toyota, Honda, Ford, GM, Hyundai — the
  Kia entries do not transfer; Tesla; all others).
- Icon-depiction details beyond the cited text (e.g. exact LKA glyph
  artwork): location + dedication carry the entries, not artwork.
- Trim/equipment-package applicability (`trim` is null on all v1
  entries): the catalog does not yet distinguish which trims carry a
  control.
- Model years outside each entry's stated range.
- Aftermarket or retrofitted controls.
- Any mapping whose certainty is `provisional` where the review needs
  `confirmed`: treat these as the next sourcing task, not as ground truth.

## Extending the catalog

1. Add an entry to `backend/audits/adas_catalog_v1.json` with every
   schema field (loader rejects partial entries).
2. Cite the official document first; record the URL actually consulted
   and set certainty honestly (`test_catalog_*` pins the schema, and
   `test_catalog_extensible_new_manufacturer` proves the path).
3. Add mapping tests (match, mismatch, downgrade) and reconcile goldens
   if verdicts change (new goldens version; never edit old cases).
4. Bump `catalog_version` and document coverage deltas here.
5. Never commit copyrighted manual pages or illustrations; treat
   downloaded documentation as untrusted data.

## Verification method (v1)

Entries were sourced from official-manual text verified through web
search excerpts and one fully fetched manual mirror (see per-entry
`accessed` dates); direct PDF fetches were blocked in that
environment, hence `provisional` throughout (plus one
`definitional`). Upgrading an entry to `confirmed` requires fetching
the official document directly and recording its revision.
