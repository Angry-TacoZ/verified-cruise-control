# PR #3 findings — Real-World ADAS Recognition & Source-Grounded Evaluation

Status: **evaluation harness complete; reference-assisted recognition
unvalidated against live model inference. Live evaluation awaits
authorization.** No live Baseten/Meta calls were made in PR #3 ($0 spend);
every number below comes from offline unit tests and the deterministic
dry-run scorer self-test unless stated otherwise.

## 1. RSA concepts extracted (M1)

`backend/audits/rsa_reference.json`, version `2026-09-rsa-v1`: 19
concepts, all `scope: "general"`, EU/IE market context, each with
source section/page, limitations, and what it can/cannot establish.
Human-readable companion: `docs/rsa-adas-reference.md`. Loader
(`backend/audits/rsa_reference.py`) rejects any non-`general` entry, so
catalog-shaped (manufacturer-specific) claims cannot slip in through
the reference path.

Families preserved from the guide (Sept 2026, 22 pp):

- **Cruise**: conventional (speed-only; CRUISE/SET telltale) vs ACC
  (radar/camera gap-keeping; car-with-distance-bars icon) vs ACC Stop
  & Go (low-speed subset). Generic cruise button proves nothing about
  ACC; a following-distance control is meaningful ACC support subject
  to identification + vehicle applicability; it never proves Stop & Go.
- **Lane**: LDW (warn-only; car-crossing-lane-lines icon) vs LKA
  (steering/brake intervention; car-between-lanes icon) vs continuous
  centering (distinct from hands-free). A general lane icon proves no
  specific level.
- **Collision**: FCW (warn) vs AEB (intervenes); rear cross-traffic
  warning vs rear auto-brake distinct. Detection hardware or a generic
  warning icon alone never proves AEB.
- **Parking**: camera vs sensors vs semi-auto steering vs full auto;
  a camera alone is not auto-parking evidence.
- **ISA** (advisory/supportive/intervening), **BSIS/BSM** (warn vs
  active assist), **driver monitoring**, **TPMS**, and the Common
  Symbols set (10 "generally standardised" icons plus the guide's own
  manufacturer-variation disclaimer — the reason mapping #4 below is
  eligibility-gated, not universal).

## 2. How they map into recognition (M2)

Optional reference-assisted mode only; baseline m2 is byte-identical
when the flag is off. Per claimed feature, `build_reference_block()`
injects one short snippet per distinct normalized feature (whole
snippets only, 2000-char cap; empty block falls back to m2). Prompt
version `m3` = m2 + version-tagged notes; execution rows record
`prompt_version` + `reference_version`. No RAG, no fine-tune, no
schema change, no catalog edits to fit benchmarks. The recheck prompt
is untouched (full-image stage only).

Manufacturer protections retained: RSA text is general knowledge;
vehicle-specific conclusions still require `adas_catalog_v1.json`
eligibility (make/model/year); unknown stays unknown; `verified`
still requires authoritative vehicle-specific confirmation.

## 3. Measured differences: baseline vs reference-assisted

**No live model comparison has been run, so no recognition gain or
regression is claimed either way.** What *is* measured:

- Scorer self-test (dry-run on the 2 committed synthetic cases, 4
  recorded vectors incl. one deliberate overclaim). The scorer now
  uses token-based `control_matches` (no empty or generic-only
  matches) and splits SAM target selection from localization, the
  latter reported as not measured without independent labels:
  control accuracy 0.50/0.50, final-state accuracy 0.50/1.00,
  unsupported-inference count 1 (baseline) / 0 (reference),
  hallucinated-positive count 0/0, `paired_cases=2`. These vectors
  prove the harness detects overclaims; they are **not** model
  performance — no inference occurred.
- Reproduce: `python evals/run.py realworld-dryrun --manifest
  fixtures/realworld-sample/manifest.json --recorded
  fixtures/realworld-sample/recorded.dryrun.json`.
- Unit level: 16 `test_realworld.py` tests (manifest validation incl.
  SHA-256 mismatch, duplicate ids, forbidden keys; scoring flags;
  paired summary; spend bound; markdown denominators) plus the 10 M1
  and 7 M2 tests — all green; full backend suite 387 tests, 0
  failures (live-smoke file: 7 skipped without keys).

## 4. Cases helped / confused by the document

Unmeasured — no authorized live runs exist, and the brief forbids
tuning on eval answers, so there is deliberately nothing here yet.
Hypothesized (to test once authorized): ACC following-distance vs
generic cruise buttons; LDW vs LKA vs centering icons; stalk/menu
controls (generic menu must not promote to a specific capability).
Risk to watch: reference snippets biasing Muse to echo the advertised
claim instead of describing visible pixels — the paired harness
measures exactly this via the unsupported-inference rate.

## 5. Real-photo dataset status (M3): infrastructure done, photos pending

Shipped: versioned manifest schema + validator (`realworld-v1`),
local-only `evals/realworld/photos/` (gitignored at three levels),
sanitized `manifest.example.json` template, 2 synthetic CI fixtures
with pinned SHA-256, dev/holdout split field (holdout tuning
forbidden). **Zero genuine photographs collected** — no suitable
rights-cleared, VIN-free, ground-truth-backed photos were available
in this pass. Reported as a shortfall, not filled with invented
cases. Target remains 12–20 cases across 4+ configurations with
vehicle-specific equipment proof (build sheet, window sticker, or
equivalent — the RSA guide is never equipment proof).

## 6. Live-eval gating (M5)

`realworld-live` refuses unless `VCC_LIVE_SMOKE=1` **and** provider
keys are configured **and** the worst-case bound (cases x modes x the
app's own per-job `spend_cap_usd`, default $2.00) fits `--max-spend`.
Every call additionally passes `EvalBudget` pre-call reservations
(call-count headroom covering bounded retries plus full per-call spend
reserve, mirroring production `reserve_call`/`settle_call`); refusals
and provider errors abort the run. Settlements book actual attempts,
provider, and cost; unknown cost freezes the ledger (never zero), and
every event flushes to a JSONL ledger so interruption preserves
history. SAM runs only with `VCC_LIVE_SAM=1`
plus a Meta key; otherwise the report records `sam_status: skipped`
and SAM metrics render "not measured", never zero. CI contains no
live path (live-smoke tests skip without keys). Preflight math is
deliberately conservative — a 2-case fixture bounds at $8.00 while
typical inference costs cents; the bound is a ceiling, not an
estimate.

## 7. Recognition gaps requiring manufacturer documents

- Catalog covers Kia Forte (2021, dash-mounted car-between-lanes
  LKA) and VW Jetta/GLI (stalk/menu assistance) patterns only; every
  other make/model/year is correctly unsupported, which caps recall
  on real-world diversity until documented mappings are added.
- RSA symbols are EU-context general knowledge; US-trim icon and
  location variants are unmapped.
- Obscured/low-resolution controls, multi-control photos, and
  model-year mismatches have harness slots but no real cases yet.

## 8. Follow-up recommendations (not implemented in PR #3)

1. Collect the pilot photo set with equipment proof (needs James
   Lane's sourcing approval; privacy review per photo).
2. Run the authorized live comparison (baseline m2 vs reference m3,
   SAM included) and publish the paired report; keep the m2 default
   if reference mode regresses.
3. Add documented catalog entries only from manufacturer sources
   (inspectable provenance, per existing `adas-catalog.md` rules) —
   never promote RSA generics into vehicle-specific mappings.
4. Consider a per-job (not per-case) live runner once the worker API
   exposes `reference_mode` per job, so evals exercise the exact
   production stage path instead of the adapter-direct path used
   here. Current live path reuses the same prompt builders and the
   production-identical observation extra (`build_eval_observation` /
   `control_extra`, incl. nested controls), the production SAM target
   selector (`sam_target_for`) with production provenance, validators,
   and reconciler — but the stage wiring differs, and that difference
   must stay labeled until unified.
5. Remediation (review of `24610e0`) hardened the live path without
   changing recognition semantics: write-ahead fsync'd spend ledger
   with restart fail-closed (F1), production SAM target selection with
   eligibility gates (F2), split selection/localization metrics (F3),
   and strict control matching (F4). All covered by deterministic
   offline regression tests; still zero paid inference.

## 9. Cross-run billing safety (pass 3 + final coordination)

Default restarts scan the results directory for same-dataset ledgers
with outstanding reservations (content-identified, including legacy
records and custom filenames) and refuse before any provider call;
per-dataset concurrency uses an OS-backed lock with no expiry while
the holder lives (OS release on process death). Recovery is manual:
reconcile billing externally, then archive or remove the ledger —
never auto-replay or rewrite. Deterministic offline regression tests;
still zero paid inference.
