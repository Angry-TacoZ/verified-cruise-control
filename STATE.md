# STATE.md — PR #3 Real-World ADAS Recognition & Source-Grounded Evaluation

Branch: `feat/004-cross-brand-evidence` (from PR #3 head `431ce9f`; PR #3 OPEN/unmerged at branch time — see PR body for base).

## PR #4 — Cross-brand ADAS evidence generalization (this branch)

Goal: Forte (dash LKA) and Jetta/GLI (stalk/menu assistance) become two
examples of location variance, not the only reasoned-about vehicles.
Generalized evidence caps at `visually_supported`; never `verified`
(Rule E) and never `contradicted`. Tiers never collapse; menu-only stays
non-diagnostic. Sources: `rsa_reference.json` (19 concepts) + existing
Forte/GLI entries; no new concepts invented. $0 spend, fake adapters only.

Step-0g inspection findings (verified in code, recorded here + PR body):
- Verdict mapping: photo-only `reconcile_claim` supporters reach at most
  `VISUALLY_SUPPORTED`; `VERIFIED`/`CONTRADICTED` require an
  `AUTHORITATIVE`-source observation. No control path reaches `verified`
  (the assumed "supporting_control and sam_ok" control branch does not
  exist in this tree) — so the structural cap lives in
  `interpret_control` (general patterns capped at `supportive`) plus the
  loader (rejects higher ceiling/strength).
- `match_entry` is lexical (specificity first); `interpret_control` did a
  single unscoped lookup, so an inapplicable specific match closed the
  case as unknown. Part A makes lookup two-phase (specific-first with
  applicability, then general scopes) with a `scopes` parameter.
- Default catalog is now `adas_catalog_v2.json` (= v1 entries
  byte-identical + 5 `general_pattern` entries, `verdict_ceiling:
  visually_supported`); v1 frozen; v2/v4 goldens pin v1, v5 exercises v2.
- `run_muse` records `prompt_version` per execution row; m4 =
  m2 + short pattern paragraph, default OFF, m2 byte-identical, m3
  unaffected. Paired-harness m2/m3/m4 comparison wired via the
  `pattern_guidance` param (harness opt-in later; `evals/run.py` live
  loop untouched per scope). No live inference run.

Status: Part A + Part B implemented, goldens v5 (14 cases) green, full
suite green, mutations a–h fail-as-required, docs updated. Draft PR #4
open (head 23ad9d3, base feat/003 at branch time); PR #3 MERGED to main
(cc3ad6f); PR #4 rebased onto main and re-verified green, base retargeted
to main. DO NOT MERGE without James Lane approval. Recognition accuracy
on real photos is STILL UNMEASURED; m4 ships OFF.

Review follow-ups (5 minor items, no verdict-behavior change):
- Ceiling-ladder fix: `contradicted` removed from the support-state ladder (support only); loader and clamp reject it fail-closed, and the clamp leaves negative findings untouched. On shipped data the clamp is a no-op guard for future ceilings, not for a future `verified` path.
- Item 1 (ceiling load-bearing): interpretation carries `verdict_ceiling`;
  `reconcile_claim` clamps Layer D support to the highest supporter
  ceiling. Goldens unchanged.
- Item 2 (`applicable_only`): phase 1 filters inapplicable specifics
  before ranking; unfiltered top match kept as the documented-unknown
  referent so v1-pinned explanations are untouched.
- Item 3: v5 goldens reformatted to v4 style (indent 2); content proven
  identical (json.loads equal).
- Item 4: stale `CATALOG_VERSION` constant deleted (unused); consistency
  test pins its absence.
- Item 5a: `--pattern-mode` opt-in third live mode (m4); default two-mode
  runs identical. Item 5b: reference+pattern combination raises
  ValueError before any provider call (row could not distinguish it
  from m4 alone).

Branch: `feat/003-real-world-adas-evaluation` (from `main` @ `97dafb3`, PR #2 merged).
Reference: RSA "Advanced Driver Assistance Systems (ADAS)", September 2026 (22 pp).
Budget: $0 authorized. No live Baseten/Meta inference without James Lane's explicit approval. No paid calls in CI, startup, imports, or unit tests.

## Milestones

- [x] M1 — Versioned structured RSA knowledge reference (`rsa_reference.json` v1, 19 concepts; loader + `guidance_for`; 10 unit tests; human-readable `docs/rsa-adas-reference.md`). PDF/pages rendered from TEMP only; nothing copyrighted committed.
- [x] M2 — Optional, versioned reference-assisted recognition mode (prompt m3; per-chunk RSA snippets ≤2000 chars, m2 fallback when unmapped; `reference_mode` param default off; usage rows record prompt+reference versions). Baseline m2 byte-identical; no RAG, no fine-tune. Recheck prompt unchanged (full-image stage only).
- [x] M3 — Dataset infra done: `realworld-v1` manifest schema + validator (`backend/audits/realworld.py`), local-only `evals/realworld/photos/` (3-level gitignore), `manifest.example.json` template, 2 synthetic CI fixtures with pinned SHA-256 + committed dry-run vectors. **Shortfall: zero genuine photographs collected** (no rights-cleared, VIN-free, ground-truth-backed photos available); 12–20 cases / 4+ configs target unmet, reported honestly.
- [x] M4 — Paired harness done: `score_case` (per-stage correctness + hallucinated/unsupported flags), `summarize` (per-mode stats, paired overlap, denominators, "not measured" states), `render_markdown` + versioned JSON reports, dev/holdout split field. Dry-run self-test on fixtures: paired_cases=2, unsupported 1/0 baseline/reference (deliberate overclaim vector detected).
- [x] M5 — Live gating done: `realworld-live` refuses without `VCC_LIVE_SMOKE=1` + keys + worst-case bound (cases x modes x per-job `spend_cap_usd` = app default $2.00) under `--max-spend`; unknown cost aborts; SAM needs `VCC_LIVE_SAM=1` or stage reports not measured. Verified refusal (no flag; bound exceeded) at $0 spend. Never in CI.
- [x] M6 — Findings in `docs/realworld-evaluation.md` (measured vs unmeasured labeled; live comparison unauthorized; follow-ups listed, none auto-implemented).

## Key decisions (see also docs/decisions/ ADRs where consequential)

- RSA guide = general knowledge source, never vehicle-specific proof; manufacturer applicability stays in `adas_catalog_v1.json` with existing eligibility gates.
- Do not overwrite Kia/VW entries from generic RSA illustrations; unknown stays unknown.
- Prompt changes versioned (m3…); every eval records exact prompt + reference versions.
- No tuning on holdout answers; unmeasured gains are never claimed (pilot framing for small n).
- Public repo: no copyrighted diagrams, dealership/user photos, VIN-linked or private data. CI uses synthetic/licensed fixtures only.

## File map (planned)

- `backend/audits/rsa_reference.json` + loader (M1; schema TBD in M1).
- Prompt builder changes in `backend/audits/pipeline.py` + `backend/providers/baseten.py` (M2).
- `evals/realworld/` manifests + harness extension in `evals/run.py` (M3/M4; live gate M5).
- `docs/rsa-adas-reference.md` (M1 human-readable) + `docs/realworld-evaluation.md` findings (M6).
- `backend/audits/realworld.py` + `backend/tests/test_realworld.py` (M3/M4; 16 tests) + `fixtures/realworld-sample/` (2 PNGs, manifest, dry-run vectors).

## RSA concepts extracted (M1 working notes)

Cruise family: conventional (set speed; speedometer or CRUISE/SET telltale) vs ACC (radar/camera gap-keeping; car-with-distance-bars icon; some stop/restart) vs Stop&Go (subset, low-speed). Lane family: LDW warn-only (car-crossing-lane-lines icon) vs LKA steering/brake intervention (car-between-lanes icon) vs continuous centering (active systems; distinct from hands-free). Collision: FCW warn (two-cars/warning icon) vs AEB auto-brake; rear cross-traffic vs rear auto-brake distinct. Parking: camera vs sensors vs semi-auto steering vs full auto (camera alone ≠ auto parking). ISA advisory/supportive/intervening. BSIS/BSM warn vs active assist. DDAW/ADDW, TPMS (horseshoe+exclamation), symbols page: 10 "generally standardised" icons + explicit manufacturer-variation disclaimer (pp. 12–21).

## Risks

- Small-n pilot evals overstate; keep pilot framing.
- Reference snippets may bias Muse toward echoing; M2 keeps "describe what is visible" + none/ambiguity handling, measures unsupported-inference rate.
- Real photos may be unavailable → ship infra + report shortfall (per brief).

## Acceptance (condensed from brief)

Draft PR open early; prompt/reference versioned; baseline reproducible; ground truth independent; local-only photos; no unauthorized media; paired comparison with denominators; mocks + regression green; backend/Ruff/Django/goldens green; mother/extension checks green; no paid inference in CI; live needs auth; no unmeasured claims; PR unmerged.

## Billing-safety defects 1–5 (5 fixes on `304ec25`)

No paid inference; no RSA/catalog/scoring/SAM-parity/frontend changes; leave PR #3 open unmerged.

- [x] D1 (P1) — default scan now blocks on ANY outstanding/settled-unknown reservation in the managed results dir, regardless of dataset (manifest edits and other datasets can no longer evade the guard); directory-wide `DatasetLock` (one fixed `.realworld-live.lock`) also closes the cross-dataset concurrency gap. `dataset_id` stays in records and refusal messages as diagnosis. Boundary documented: one managed results dir per provider account. Adjusted `test_different_dataset_not_blocked` into blocking (old per-dataset behavior retired); added added-case, other-dataset, and cross-dataset-lock tests.
- [x] D2 (P2) — only non-empty string tokens enter the settled set; a reserved record with a missing/None/non-string token is always unresolved.
- [x] D3 (P2) — `read_ledger` validates every line (dict + known event, else ValueError→BudgetRefused); a custom file whose first line is a non-object now fails closed before `read_ledger` is reached. Tests cover conventional + custom names, first and later lines, with lock release and zero provider calls.
- [x] D4 (P3) — setup after acquisition runs inside the guarded try; nested finally keeps `close()` failure from skipping release; `LedgerFile` closed only if created.
- [x] D5 (P3) — new-file creation fsyncs the parent dir on POSIX (`_fsync_parent`; skipped on `nt` with comment), for ledgers and the lock file; best-effort crash consistency documented, per-event file fsync unchanged. POSIX wiring tests skip on Windows (CI covers them); Windows-path msvcrt behavior unchanged and covered locally.

## Remaining billing-safety issues (2 items on `907d14c`)

No paid inference; no RSA/catalog/scoring/SAM-parity/frontend changes; leave PR #3 open unmerged.

- [x] Issue 1 — `LedgerFile` construction is fail-closed: an `open()` failure raises `BudgetRefused`; a parent-dir fsync failure closes the new handle then raises `BudgetRefused` naming durable-creation uncertainty (empty file left in place, blocks nothing). run.py converts ledger-creation refusal into SystemExit "refusing: ..." like every other pre-inference gate. Testable `_should_fsync_parent()` predicate replaces the inline `os.name` check (Windows skip unchanged). Docstrings state refusal, not best-effort.
- [x] Issue 2 (+2b) — CLI verified to expose no results-dir option and to pass none (regression test asserts both, including argparse rejection of `--results-dir`); docs state the single-checkout boundary and the extension limitation (only top-level `*.jsonl` scanned; keep `.jsonl` when archiving, never rename to disable).

## Remediation pass 3 (review of `18349d2`, 1 finding — prior head CI 4/4 green)

No paid inference; no RSA/catalog/prompt/ground-truth changes; leave PR #3 open unmerged.

- [x] Cross-run billing safety (MEDIUM) — default command mints a fresh ledger per run and only checks `--ledger` files, so restarting an interrupted evaluation without `--ledger` bypasses unresolved reservations. Fix without redesigning the budget system: content-based dataset fingerprint in every record; default runs auto-scan the ledger dir for same-dataset (plus legacy/ambiguous) outstanding reservations and refuse before any provider request; atomic per-dataset lock file; explicit `--ledger` behavior preserved; documented manual recovery (reconcile, archive, never auto-replay/rewrite). Tests A–G with fakes + tmp dirs, zero network.

## Final billing coordination (2 corrections on `ac6a117`)

No paid inference; no RSA/catalog/recognition/eval-feature changes; leave PR #3 open unmerged.

- [x] F1 (P1) — replaced the 600s-TTL lock with OS-backed `DatasetLock`: exclusive lock on an fd held open for the whole run (msvcrt on Windows, flock on POSIX), so a live run can never lose ownership by duration; OS releases on process death for crash recovery; in-process double-acquire refuses; lock file left on disk (diagnostics only). Windows learning: handle teardown can lag process exit slightly, so the crash-recovery test polls briefly after kill. Tests: stale/garbage content never blocks, cross-process hold-then-kill-then-recover.
- [x] F2 (P2) — ledger discovery is now content-based: convention-named files plus any other `*.jsonl` whose first record carries eval-ledger identity (event/token/run_id), so custom filenames cannot evade the scan; well-formed non-ledger JSON and empty files ignored; unreadable/unparseable candidates fail closed; `archive/` excluded by non-recursive scan; ledgers moved outside the results dir are no longer discovered (documented). Tests: custom-filename same-dataset block before any provider call, torn custom ledger fails closed.
- [x] P1 ledger check/lock race — the authoritative scan now runs *under the held dataset lock* (lock first, then scan, release on refusal): a reservation landing between scan and lock acquisition can no longer authorize execution on stale accounting. Refusal paths release the lock; the run holds it throughout paid evaluation. Tests: race-planted outstanding ledger caught with zero provider calls, settled race ledger does not block, refused run releases the lock.

## Remediation pass 2 (review of `24610e0`, 2 findings)

No paid inference; no RSA/catalog/photo changes; leave PR #3 open unmerged.

- [x] F1 (HIGH, durable ledger) — write-ahead append-only `LedgerFile` (flush + fsync per event; never rewrite); reservation fsync'd before dispatch, settlement after; run ids on every record; `fresh_ledger_path` per run; restart via `assert_ledger_clear` fails closed on outstanding (no auto-replay); unknown cost freezes; `--ledger` resume flag. Disk-reading tests: on-disk-before-analyze, persistence-failure blocks call, crash→refuse, settlement persisted, no-overwrite, identity hygiene.
- [x] F2 (MEDIUM, SAM parity) — `sam_target_for` renamed public and reused via `select_sam_target` (PRESENT + `supports_localization` eligibility, else no call); `build_sam_eval_observation` carries production provenance (same photo, source id, control index); reconcile sees `[muse, sam]` rows; `unavailable-target` recorded when no target. Scenarios A-D green incl. end-to-end harness test with fake adapters (zero SAM calls, zero spend).

## Remediation pass (review of `ef36a02`, 4 findings)

Work on `feat/003-real-world-adas-evaluation`; no paid inference; no catalog/RSA broadening.

- [x] F1 (HIGH, budget safety) — `EvalBudget` pre-call reservation ledger in `backend/audits/realworld.py`: authorization gate, call-count headroom (first try + bounded retries), full per-call spend reserve, unknown-cost freeze, append-only records + JSONL flush after every event (finally-block, so interruption preserves history). `run_realworld_live` routes all Muse/SAM calls through `budget.guarded()`; refusals and provider errors abort via `SystemExit`. 6 regression tests, all deterministic with fake adapters (unauthorized/over-budget/unknown-freeze/ledger-durability/actuals).
- [x] F2 (HIGH, production parity) — `build_eval_observation()` builds unsaved `Observation`s with the production-identical extra (`supports_localization`/`needs_crop`/`needs_other_view` + `control_extra()` projection: symbols, locations, dedicated, manufacturer context, nested `controls`); `_control_extra` renamed public for reuse. Live runner reconciles these instead of `visible_object`-only rows; empty model output records `missing-observation`, never a fabricated row. Regression test: nested ACC diagnostic control flips `insufficient_evidence` to `visually_supported` on a Forte 2021 ACC claim. Baseline m2 / reference m3 versions unchanged.
- [x] F3 (MEDIUM, SAM metric) — split into `sam_target_selected` (text agreement via `control_matches`, labeled selection-only) and `sam_localization_correct` (scored only against optional independent `ground_truth.localization.verified_outcome` label + `verified_by`; otherwise `None` → report renders "not measured"). Markdown shows both rows. Committed fixtures carry no GT labels, so localization reads not measured.
- [x] F4 (MEDIUM, control-match) — `control_matches()`: normalized exact match, else equal substantive token sets, else subset with ≥2 shared tokens; empties and generic-only overlap (`button`, `control`, `icon`, … stopwords) never match. 3 regression tests (empty GT, single-generic-token, case/whitespace/subset/near-miss).
