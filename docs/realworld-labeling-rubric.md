# Real-world labeling rubric (photo-only pilot)

Labels are assigned by a human reviewer from independently established
evidence, NEVER by running `reconcile_claim()` and copying its output.
"Independent" means independent of model outputs: the reviewer has not
seen any model prediction for the case. Record disagreements between
reviewers in the register `status_reason`; do not silently resolve them.

## Inputs

- `equipment`: independently established vehicle truth — `equipped` /
  `not-equipped` / `unknown` (window sticker, build sheet, or verified
  equipment record; never generic model-year specs, never the RSA guide).
- `interpretation`: what the visible object says about the advertised
  claim — `supports` (dedicated, documented control) / `generic`
  (ordinary control shared with lesser capabilities) / `insufficient`
  (no useful object) / `unknown` (cannot tell).
- `expected_state`: the evidence state the PHOTO-ONLY evaluation can
  legitimately support. The photo-only pipeline has no authoritative
  evidence input, so `verified` and `contradicted` are unreachable here
  (enforced by the admission validator). A `not-equipped` vehicle
  labeled `contradicted` would mark a correct `insufficient_evidence`
  prediction wrong, so do not do it.

## Decision table

| Equipment | Visible object | Interpretation | Expected state |
|---|---|---|---|
| equipped / unknown | dedicated control (e.g. ACC distance button, documented LKA button) | supports | `visually_supported` |
| not-equipped | conventional cruise buttons where ACC is claimed | generic | `insufficient_evidence` (a `visually_supported` prediction is penalized via `hallucinated_positive`) |
| any | ordinary/shared control, menu-only toggle (e.g. VW-style assistance menu) | generic | `insufficient_evidence` |
| any | no relevant control visible, obscured/unreadable icon, ambiguous symbol | insufficient / unknown | `insufficient_evidence` |
| any | claim needs non-visual confirmation (behavioral/software capability, e.g. Stop & Go low-speed resume, AEB intervention logic) | insufficient / unknown | `not_visually_verifiable` |
| any | only sensor housings / camera pods visible, no control or indicator | insufficient | `insufficient_evidence` (housings do not establish software capabilities) |

## Worked example

ACC claim, vehicle confirmed NOT to have ACC, photo shows a
conventional cruise button: equipment `not-equipped`, interpretation
`generic`, expected_state ordinarily `insufficient_evidence`.

## Tier discipline (from Rule E)

LKA ≠ lane centering ≠ hands-free; conventional cruise ≠ ACC; a menu
control never establishes a specific capability; a sticker proving
absence is not a `contradicted` label; a document proving presence is
not a `verified` label — documents establish `equipment`, photos
establish `expected_state`, and the two stay separate.
