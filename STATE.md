# STATE.md — PR #3 Real-World ADAS Recognition & Source-Grounded Evaluation

Branch: `feat/003-real-world-adas-evaluation` (from `main` @ `97dafb3`, PR #2 merged).
Reference: RSA "Advanced Driver Assistance Systems (ADAS)", September 2026 (22 pp).
Budget: $0 authorized. No live Baseten/Meta inference without James Lane's explicit approval. No paid calls in CI, startup, imports, or unit tests.

## Milestones

- [x] M1 — Versioned structured RSA knowledge reference (`rsa_reference.json` v1, 19 concepts; loader + `guidance_for`; 10 unit tests; human-readable `docs/rsa-adas-reference.md`). PDF/pages rendered from TEMP only; nothing copyrighted committed.
- [ ] M2 — Optional, versioned reference-assisted recognition mode (prompt m3?); baseline PR #2 behavior preserved and reproducible; no RAG, no fine-tune.
- [ ] M3 — Real-world evaluation dataset infra (local-only photos; sanitized versioned manifests; 12–20 cases / 4+ configs target; report shortfall honestly).
- [ ] M4 — Paired baseline-vs-reference eval harness (identical cases; per-stage metrics; machine + human reports; dev/holdout split; no tuning on holdout).
- [ ] M5 — Live-eval gating (dry-run default; explicit auth + spend bounds required; versioned outputs; provider error handling).
- [ ] M6 — Findings documentation (what helped, what confused, gaps, follow-ups). No auto-implemented improvements.

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
- `docs/rsa-adas-reference.md` (M1 human-readable) + findings doc (M6).

## RSA concepts extracted (M1 working notes)

Cruise family: conventional (set speed; speedometer or CRUISE/SET telltale) vs ACC (radar/camera gap-keeping; car-with-distance-bars icon; some stop/restart) vs Stop&Go (subset, low-speed). Lane family: LDW warn-only (car-crossing-lane-lines icon) vs LKA steering/brake intervention (car-between-lanes icon) vs continuous centering (active systems; distinct from hands-free). Collision: FCW warn (two-cars/warning icon) vs AEB auto-brake; rear cross-traffic vs rear auto-brake distinct. Parking: camera vs sensors vs semi-auto steering vs full auto (camera alone ≠ auto parking). ISA advisory/supportive/intervening. BSIS/BSM warn vs active assist. DDAW/ADDW, TPMS (horseshoe+exclamation), symbols page: 10 "generally standardised" icons + explicit manufacturer-variation disclaimer (pp. 12–21).

## Risks

- Small-n pilot evals overstate; keep pilot framing.
- Reference snippets may bias Muse toward echoing; M2 keeps "describe what is visible" + none/ambiguity handling, measures unsupported-inference rate.
- Real photos may be unavailable → ship infra + report shortfall (per brief).

## Acceptance (condensed from brief)

Draft PR open early; prompt/reference versioned; baseline reproducible; ground truth independent; local-only photos; no unauthorized media; paired comparison with denominators; mocks + regression green; backend/Ruff/Django/goldens green; mother/extension checks green; no paid inference in CI; live needs auth; no unmeasured claims; PR unmerged.
