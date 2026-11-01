# Evaluation methodology

Reproducible evaluation for every AI, extraction, reconciliation, ranking,
or verification change (AGENTS.md Rule D). PR #1 establishes the harness
shape; scored baselines land with PR #2's first inference.

## Layout

```
evals/
├── README.md        # this file
├── cases/           # versioned eval cases (inputs + expected outputs)
└── results/         # raw runs: <date>-<model>-<prompt-version>.json
```

Cases and results are committed so regressions are reviewable diffs. Use
synthetic or properly licensed data only — never private listings.

## Protocol

1. **Baseline first**: document the current implementation's scores before
   changing it. A candidate without a baseline comparison is not shippable.
2. **Fixed dataset**: cases are versioned (`cases/v1/…`); never edit a case
   to improve a score — add a new version and note why.
3. **Identical runs**: same cases, same harness, baseline vs candidate;
   preserve raw evidence + outputs in `results/`.
4. **Compare + decide**: record regressions, failures, retained/reverted
   changes, and actual model usage/cost per provider pool.

## Metrics

ADAS claim-extraction accuracy · unsupported-claim recall · contradiction
precision · visual-evidence localization accuracy · unsupported-inference
rate · human-escalation rate · reviewer-correction rate · API latency ·
cost per listing · malformed-input reliability.

## Current status

- `cases/v4/reconcile_goldens.json`: 13 hand-labeled reconcile cases
  with explicit audit-vehicle blocks; run with
  `python evals/run.py reconcile` (zero inference). Pinned in CI by
  `backend/tests/test_eval_goldens.py` alongside v2. v4 supersedes v3
  with eligibility gating (manufacturer-specific entries apply only to
  documented make/model/year vehicles; borrowed entries, out-of-range
  years, and missing identity yield `insufficient_evidence`) plus
  per-control aggregation (nested controls support independently; SAM
  aims at the claim-relevant item). v1/v2/v3 cases and results are
  preserved as versioned history.
- `cases/v3/reconcile_goldens.json`: 13 hand-labeled reconcile cases
  over manufacturer-aware interpretation without vehicle identity;
  superseded by v4, retained for history.
- `cases/v2/reconcile_goldens.json`: 14 hand-labeled reconcile cases
  under the pre-catalog conservative classifier (absence is never
  contradiction); still pinned. v1 retained for history.
- `cases/v1/muse_fixture_ground_truth.json`: unsupported-inference eval
  over the 3 synthetic fixture photos (no honest output may claim a
  feature is present). Run with `python evals/run.py muse-live`: one live
  call, refuses unless `VCC_LIVE_SMOKE=1` plus keys, cost bound $0.25.
  Live runs need explicit human approval and are never run in CI.
- `results/2026-10-31-smoke-baseten-meta.json`: provider connectivity
  smoke baseline (not a scored eval).
