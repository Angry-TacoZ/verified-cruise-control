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

Optional `source_group` (opaque id shared by all cases from one
physical vehicle) is tolerated by the schema and REQUIRED by pilot
admission. See `INTAKE.md` for the operator checklist and
`docs/realworld-labeling-rubric.md` for the labeling rules.

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

# Zero-inference intake validation (schema, value-level privacy scan,
# SHA-256 verification, cross-split/duplicate checks, holdout freeze
# check, quality reports; no provider calls):
python evals/run.py realworld-validate --manifest fixtures/realworld-sample/manifest.json

# With a linked private register and a second split (dev vs holdout):
python evals/run.py realworld-validate --manifest evals/realworld/manifest.pilot.json \
  --other-manifest evals/realworld/manifest.dev.json --register <private>/register.json \
  --enforce-freeze --public-report

# Freezing a holdout (refuses fail-open freezes; needs dev + photos):
python evals/run.py realworld-freeze --manifest <holdout>.json \
  --other-manifest <dev>.json --reviewer-role dataset-reviewer
```

# Live (needs explicit James Lane approval + VCC_LIVE_SMOKE=1):
python evals/run.py realworld-live --manifest evals/realworld/manifest.pilot.json --max-spend 5.00
```

Live runs refuse without `VCC_LIVE_SMOKE=1`, configured provider keys,
and a worst-case spend bound (cases x modes x per-job cap) under
`--max-spend`. Every call additionally passes `EvalBudget` pre-call
reservations (attempt + spend headroom, unknown-cost freeze) recorded
to a write-ahead JSONL spend ledger (flushed + fsync'd per event, one
file per run); restarting against a ledger with outstanding
reservations refuses — pass `--ledger <path>` only for a clean trail.
The default restart also scans the whole results directory for ANY
ledger with outstanding or unknown-cost reservations, regardless of
dataset — spend already left the provider account, so a renamed
manifest, an added case, or a different dataset cannot evade the
check — identified by ledger content (not filename), performed while
holding the directory lock so a reservation landing mid-startup cannot
slip through; unrelated `.jsonl` files are ignored. Concurrency is
gated by one OS-backed directory-wide lock held open for the whole
run — it cannot expire while the holder lives, and the OS releases it
if the holder process dies. After a crash, allow a few seconds for OS
handle teardown, reconcile provider billing externally, then archive
(move to `archive/`, which the scan skips) or remove the ledger before
rerunning. Ledgers moved outside the results directory are no longer
discovered.
Boundary: the lock and scan guard the evaluations sharing one managed
results directory — the single repo checkout's evals/results. Separate
checkouts, clones, or machines using the same provider accounts are NOT
coordinated: do not run paid evaluations from two checkouts
concurrently, and reconcile billing from every checkout before rerunning.
Likewise, a ledger renamed to another extension (.bak, .json, .txt) is
not discovered: the scan only reads top-level *.jsonl. Archive reviewed
ledgers into archive/ keeping the .jsonl extension; never rename
extensions to "disable" a ledger.
SAM localization additionally requires `VCC_LIVE_SAM=1`; its target
comes from the production selector over the full observation (nested
controls included) with production provenance, and without a valid
target no SAM call is made (`unavailable-target`). Without SAM, the
stage is recorded as not measured, not as zero.
Never run live evals in CI.
