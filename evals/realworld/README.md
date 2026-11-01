# Real-world evaluation dataset (local-only)

This directory holds **versioned manifests** describing real-photograph
evaluation cases. Photographs themselves are **never committed** to the
public repository.

## Layout

```
evals/realworld/
├── README.md              # this file
├── manifest.example.json  # sanitized template (placeholder SHA, no bytes)
└── photos/                # GITIGNORED local overlay (see below)
```

Local manifests live here as `manifest.<name>.json` (also gitignored
unless explicitly approved); only `manifest.example.json` is committed.

## Manifest schema (`realworld-v1`)

Implemented and enforced by `backend/audits/realworld.py`
(`validate_manifest`) and covered by `backend/tests/test_realworld.py`.

Top level: `manifest_version` (`"realworld-v1"`), `dataset` (label),
`split` (`"dev"` or `"holdout"` — never tune on holdout, never report
dev numbers as validation), `notes`, `cases` (non-empty list).

Per case: `id`, `vehicle` (`year`/`make`/`model`/`trim`/`market`),
`claim` (`advertised` seller wording preserved verbatim,
`normalized_feature`, `claim_id`), `photo` (`path` relative to the
photos root, `sha256` hex digest, `provenance`), `ground_truth`
(`equipment`: `equipped`/`not-equipped`/`unknown`; `visible_object`
independently confirmed; `interpretation`: `supports`/`generic`/
`insufficient`/`unknown`; `expected_state` one of the five taxonomy
states; `verification_method`; `ambiguity`).

Manifests must not contain VINs, plates, owner data, or image bytes —
validation rejects them.

## Hygiene rules (binding)

- No dealership photographs, user-provided photographs, copyrighted
  diagrams, VIN-linked records, or private metadata without rights and
  authorization — in the repo, in issues, or in PRs.
- CI runs synthetic or properly licensed fixtures only
  (`fixtures/realworld-sample/`, `evals/cases/`).
- The RSA guide is reference material, never ground truth for an
  individual vehicle's equipment. Equipment truth requires
  vehicle-specific sources (build sheet, window sticker, documented
  equipment).
- `photos/` is gitignored; manifests record SHA-256 so a local photo
  set can be verified without being published.

## Running

```sh
# Zero inference, always safe (validates + scores recorded outputs):
python evals/run.py realworld-dryrun --manifest fixtures/realworld-sample/manifest.json

# Live (needs explicit James Lane approval + VCC_LIVE_SMOKE=1):
python evals/run.py realworld-live --manifest evals/realworld/manifest.pilot.json --max-spend 5.00
```

Live runs refuse without `VCC_LIVE_SMOKE=1`, configured provider keys,
and a worst-case spend bound (cases x modes x per-job cap) under
`--max-spend`. Every call additionally passes `EvalBudget` pre-call
reservations (attempt + spend headroom, unknown-cost freeze) with a
JSONL spend ledger. SAM localization additionally requires `VCC_LIVE_SAM=1`;
without it the SAM stage is recorded as not measured, not as zero.
Never run live evals in CI.
