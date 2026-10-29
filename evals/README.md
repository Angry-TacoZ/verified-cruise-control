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

No scored runs yet — PR #1 performs zero model calls by design. PR #2 will
add `cases/v1/` (fixed ADAS extraction + localization cases) and the first
baseline + candidate result files with real Baseten/Meta usage.
