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
5. Two-phase precedence: an applicable manufacturer-specific entry
   always wins over a general pattern. An inapplicable specific entry
   falls through to the general scopes (`general`, `general_pattern`)
   instead of closing the case as unknown; only when no general entry
   matches either does the case resolve to documented unknown (naming
   the inapplicable specific entry where one matched lexically).
6. General-pattern matches are structurally capped at `supportive`
   strength in `interpret_control` (in addition to the loader rejecting
   higher ceilings/strengths), and the ceiling is load-bearing: each
   supporting interpretation carries its entry's `verdict_ceiling`, and
   `reconcile_claim` clamps the resulting evidence state to the highest
   ceiling among the supporters (entries without a ceiling are
   unaffected). Shipped photo evidence therefore reaches at most
   `visually_supported` on general patterns — never `verified`,
   never `contradicted`. On shipped data the clamp is a no-op
   (general patterns already map to `visually_supported` at the
   single clamp point, and no shipped entry declares a lower
   ceiling): it guards future ceilings, not a future `verified`
   path. The ceiling ladder covers support states only —
   `verified` > `visually_supported` > `insufficient_evidence` >
   `not_visually_verifiable` — and `contradicted`, a negative
   finding rather than weaker support, can never be a ceiling.
   A documented menu/non-diagnostic entry is never upgraded by a
   general pattern.
6. Feature mismatch (entry maps to X, claim is Y) → does not support,
   with the distinction spelled out; `cannot_infer` lists what the
   evidence cannot establish.

Strengths: `diagnostic` / `supportive` (both can support a claim) /
`ambiguous` / `non_diagnostic` / `unknown` (none support).

## Entry scope and vehicle eligibility

Each entry declares `scope`:

- `manufacturer_specific`: applies only when the audit vehicle's
  trusted listing metadata (year/make/model/trim from the queue-time
  snapshot) falls inside the entry's documented applicability — make
  equality, model match (case-insensitive containment, so "Forte"
  covers "Forte5" but never "K5"), model year inside the entry's
  range, and trim match when the entry documents one. Unknown or
  uncertain identity rejects the entry. A conflicting make/model/year
  rejects it; there is no partial credit.
- `general`: applies to any vehicle, including unknown ones. Only
  entries explicitly documented as cross-manufacturer may use this
  scope (currently just the definitional conventional-cruise entry).
- `general_pattern` (catalog v2): cross-manufacturer visual patterns
  (dedicated LKA/LDW controls, ACC distance controls, centering
  controls, assistance menus) that apply to ANY vehicle, including
  unknown identity. Each such entry declares
  `verdict_ceiling: "visually_supported"`, and the loader rejects any
  general-pattern entry with a higher ceiling or a strength above
  `supportive`. The interpretation layer re-enforces the cap in code,
  and labels supporting explanations as a general cross-manufacturer
  pattern requiring reviewer confirmation. Manufacturer-specific
  entries stay the only route to `diagnostic`/verified-grade evidence
  and win on precedence whenever one applies.

Muse's optional visual manufacturer guess never establishes or
overrides eligibility: listing metadata governs, and a guess that
names neither the listed make nor model is recorded as an explicit
conflict note without changing the outcome. The VIN is never used;
its absence never invalidates known year/make/model metadata.

## Per-control evidence

One observation may carry several distinct controls (top-level object
plus the validated nested `controls` array, at most 4). Each becomes
an individually traceable evidence item (index 0 = top-level);
nested items inherit only the photo-level manufacturer context, and a
nested restatement of the top-level control is deduplicated. Every
item is interpreted independently, all interpretations are retained
in the finding evidence, and SAM localizes the claim-relevant item
(supporting first, then feature-matching) — one SAM call per
(claim, photo) as before, just aimed at the right object. The
primary explanation always comes from an interpretation that
actually supports the verdict; with no supporters it states the
strongest relevant limitation.

## Certainty levels (reference applicability, not probabilities)

- `confirmed`: official manufacturer document directly verified.
- `provisional`: manual mirror, excerpt, or third-party hosting; the
  document is official but this project has not fetched the original.
- `definitional`: true by control semantics (e.g. set/resume-only
  buttons cannot be ACC-specific), not by manual lookup.

## Shipped entries (catalog 2026-11-03/v2)

Catalog v2 keeps every v1 entry byte-identical and appends five
`general_pattern` entries (RSA ADAS guide, September 2026, plus the
Forte/GLI location-variance examples). Default catalog for production
is v2; `adas_catalog_v1.json` is frozen and stays available by path
for the pinned v2/v4 golden sets.

| Entry | Scope | Vehicle | Control | Maps to | Strength |
|---|---|---|---|---|---|
| `kia-forte-2021-scc-distance` | specific | Kia Forte 2021–2022 | Vehicle Distance steering-wheel button (SCC) | adaptive_cruise_control | diagnostic |
| `kia-forte-2019-2024-lka-button` | specific | Kia Forte 2019–2024 | LKA button, instrument panel lower-left + cluster indicator | lane_keeping_assist | diagnostic |
| `vw-jetta-gli-assist-stalk` | specific | VW Jetta/GLI 2021–2023 | Stalk-end assistance button + infotainment menu (opener) | none (menu) | non_diagnostic |
| `generic-cruise-set-res` | general | any | ON/SET/RES buttons, no distance function | cruise_control | non_diagnostic |
| `kia-lfa-steering-buttons` | specific | Kia K5/Seltos/Forte 2021–2024 | Lane Following Assist wheel controls | lane_centering_assist | supportive |
| `vw-lane-assist-naming` | specific | VW Jetta/GLI/Golf 2021–2026 | naming only (no control) | lane_keeping_assist | supportive |
| `general-lka-dedicated-control` | general_pattern | any | dedicated LKA button/indicator (vehicle-between-lanes / LKA wording); wheel, dash, stalk, cluster, console | lane_keeping_assist | supportive (capped) |
| `general-ldw-warning-control` | general_pattern | any | dedicated LDW control (car-crossing-lines / departure wording) | lane_departure_warning | supportive (capped) |
| `general-assistance-menu` | general_pattern | any | assistance button/menu opener (menu, not a capability control) | none (menu) | non_diagnostic |
| `general-acc-distance-control` | general_pattern | any | dedicated following-distance/gap control or distance-bars indicator | adaptive_cruise_control | supportive (capped) |
| `general-centering-wheel-control` | general_pattern | any | control with explicit centering/following wording | lane_centering_assist | supportive (capped) |

Sources (doc identity, URL actually consulted, section, access date) are
recorded per entry in `backend/audits/adas_catalog_v1.json` and the
appended v2 entries in `backend/audits/adas_catalog_v2.json` (RSA
section + page per concept, e.g. 6.4/p.16 for lane assistance,
6.1/p.13 for cruise tiers, plus the Forte/GLI location-variance
citations). No manual illustrations are reproduced; visual fixtures
are generated schematics under `fixtures/adas/`.

## What general patterns can and cannot establish

General patterns deliberately cover other manufacturers (Toyota,
Honda, Ford, GM, Hyundai, Tesla, and any unlisted or unknown brand):
a dedicated LKA button on a Camry steering wheel, for example, is
recognized through the general LKA pattern rather than the
Forte-specific entry, capped at `visually_supported`.

They can establish:
- that a dedicated control of a documented visual family supports the
  matching capability at `visually_supported` (never higher);
- that a menu-only control supports nothing (`non_diagnostic`);
- tier separation: LKA evidence never promotes to centering, cruise
  evidence never promotes to ACC, and absence of a visible control is
  `insufficient_evidence`, never `contradicted`.

They cannot establish:
- `verified` (needs authoritative vehicle-specific evidence, Rule E);
- `contradicted` (absence of a visible control is not contradiction);
- fitment for a specific vehicle (reviewer confirms against the
  actual vehicle);
- any capability beyond the matched tier (`cannot_infer` lists the
  adjacent tiers explicitly).

## Still unsupported / unverified

- Icon-depiction details beyond the cited text (e.g. exact LKA glyph
  artwork): location + dedication carry the entries, not artwork.
- Icon-depiction details beyond the cited text (e.g. exact LKA glyph
  artwork): location + dedication carry the entries, not artwork.
- Trim/equipment-package applicability (`trim` is null on all v1
  entries): the trim gate is implemented and tested, but no shipped
  entry documents trim-level applicability yet.
- Model years outside each entry's stated range.
- Aftermarket or retrofitted controls.
- Any mapping whose certainty is `provisional` where the review needs
  `confirmed`: treat these as the next sourcing task, not as ground truth.

## Extending the catalog

1. Add an entry to `backend/audits/adas_catalog_v2.json` with every
   schema field (loader rejects partial entries), including `scope`:
   `manufacturer_specific` entries must document make/model/year;
   `general` entries must document why the mapping is genuinely
   cross-manufacturer; `general_pattern` entries must additionally
   declare `verdict_ceiling: "visually_supported"`, use a strength no
   higher than `supportive`, cite `rsa_reference.json` concept ids
   with section + page (plus Forte/GLI entries for location-variance
   examples), and declare `cannot_infer` for the adjacent tiers.
   (`adas_catalog_v1.json` is frozen; do not edit it.)
2. Cite the official document first; record the URL actually consulted
   and set certainty honestly (`test_catalog_*` pins the schema, and
   `test_catalog_extensible_new_manufacturer` proves the path).
3. Add mapping tests (match, mismatch, downgrade, precedence, cap)
   and reconcile goldens if verdicts change (new goldens version;
   never edit old cases; v4 goldens stay pinned to catalog v1).
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
