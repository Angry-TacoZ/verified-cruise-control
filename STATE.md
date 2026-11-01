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
