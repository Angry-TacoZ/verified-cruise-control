# STATE.md — PR #3 Real-World ADAS Recognition & Source-Grounded Evaluation

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

## Remediation pass 3 (review of `18349d2`, 1 finding — prior head CI 4/4 green)

No paid inference; no RSA/catalog/prompt/ground-truth changes; leave PR #3 open unmerged.

- [ ] Cross-run billing safety (MEDIUM) — default command mints a fresh ledger per run and only checks `--ledger` files, so restarting an interrupted evaluation without `--ledger` bypasses unresolved reservations. Fix without redesigning the budget system: content-based dataset fingerprint in every record; default runs auto-scan the ledger dir for same-dataset (plus legacy/ambiguous) outstanding reservations and refuse before any provider request; cross-process lock file with TTL takeover (no new infra); explicit `--ledger` behavior preserved; documented manual recovery (reconcile, archive, never auto-replay/rewrite). Tests A–G with fakes + tmp dirs, zero network.

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
