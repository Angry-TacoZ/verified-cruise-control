# PR #2 implementation plan: Multimodal ADAS Audit Engine

Branch: `feat/002-multimodal-audit` (from `main` @ `fa82309`, merged PR #1).
Status: pre-implementation review complete; plan only — no product code yet.

## 1. API verification gate (done, one item open)

Researched against official docs on 2026-10-31. No inference calls made.

**Baseten / Muse Spark Open (vision-language).**
OpenAI-compatible `POST https://inference.baseten.co/v1/chat/completions`,
Bearer `BASETEN_API_KEY`. Images via `image_url` (public URL or base64 data
URI) in a multimodal content part. JSON mode supported. Caution, from
Baseten's own structured-outputs doc: Muse Spark Open **accepts
`json_schema` but does not enforce it** — server-side schema enforcement
cannot be relied on, so strict client-side validation (already required by
the brief) is the real contract. Pricing seen 2026-10-31: $1.25 input /
$4.25 output per 1M tokens (record pricing version + date in usage rows).

**Open item — exact slug.** Official Baseten docs list
`meta-models/muse-spark-open`; the mandated `meta-models/muse-spark-open-1026`
appears in third-party hackathon usage but is not confirmed in official docs.
Resolution: model id becomes a Django setting defaulting to the mandated
slug; the bounded live smoke test (needs James's approval) confirms it. If
it 404s, ADR + switch to the documented slug — same model, not a
substitution. Until the smoke test runs, all inference stays mock-backed.

**Meta Model API / SAM 3.1 (segmentation).** Verified against
`dev.meta.ai/docs/sam/*`: `POST https://api.meta.ai/v1/responses`
(Responses API, SSE stream to `response.completed`), `model: "sam-3.1"`.
One `input_text` (a single short noun phrase, or box/point token grammar)
plus one `input_image` whose `image_url` accepts a **base64 data URL** —
local originals never need public hosting. Output is a documented token
grammar (`<0f>id<|box;…|><|mask;…|>`, media-pixel boxes, base85 mask
payload); zero matches completes with empty output (normal, not an error).
Pricing seen 2026-09-20: $2.50 / 1,000 images (recheck at deploy).

**Transport decision (implemented).** Added `httpx==0.28.1` (sync client,
timeouts, streaming) plus `meta-sam-parser==0.1.0` — Meta's official SAM
output parser. Revision rationale: the mask codec is an arithmetic-coded
bitstream (verified by reading the package); hand-rolling it would be the
risky move. Scope is tight: Meta's package is used ONLY for mask
payload→raster decoding. SSE handling, record-grammar parsing, bounds
checks, and Muse JSON validation are our own strict layer. SAM License
checked: no commercial/field-of-use restrictions (sanctions compliance
only). The `openai` SDK was not adopted.

## 2. Work breakdown + acceptance criteria

Each item ships with tests; nothing merges without the §5 gates.

1. **Provider adapters (live paths).** `MuseRequest` gains image bytes →
   base64 data-URI content parts; `SamRequest` gains image bytes + concept
   phrase / box-point prompts → Responses payload; strict response
   validators; error mapping (401/403/404-model/auth, 408/429/timeout,
   malformed). AC: contract tests against recorded (redacted) fixtures;
   malformed payloads quarantined, never persisted as findings.
2. **Config + credit safety.** Settings for enablement, per-audit call cap,
   per-audit spend cap, crop/recheck limits; fail-closed when cost is
   unbounded; explicit missing-credential errors. AC: inference impossible
   unless explicitly enabled + operator-triggered; caps enforced in tests.
3. **`audits` app + migrations.** `AuditJob` (queued/running/completed/
   partial/failed/cancelled, stage, progress, owner/heartbeat, config +
   prompt versions, photo SHA snapshots), `ModelExecution`, `PhotoObservation`,
   `LocalizedEvidence`, `AuditFinding`, persisted usage ledger (Decimal,
   separate pools, unknown-not-zero cost). AC: migration checks; audit
   snapshot immutable after listing changes.
4. **Worker (`run_audit_worker`).** Separate process; atomic SQLite claim;
   short transactions, no locks across network; stage checkpoints; reuse of
   completed executions (no duplicate paid calls); safe-boundary cancel;
   abandoned-job detection via owner/heartbeat (no pure timeouts). AC:
   concurrency tests (no double-claim), restart/recovery tests, cancel tests.
5. **Pipeline.** Deterministic screening (reuse Pillow/SHA code; conservative,
   nothing permanently discarded) → Muse full-image → SAM targeted → conditional
   crop recheck (bounded) → reconcile. Derived media under
   `runtime-data/derived/{masks,crops,thumbnails}` with backend ids and
   provenance. AC: stage outputs inspectable after failure; originals untouched.
6. **Reconciliation (pure, no model calls).** Exact Rule E taxonomy + all ten
   safeguards as coded rules; synonym map preserving original wording;
   review-required flags; no invented confidence. AC: rule tests incl. every
   safeguard; taxonomy pinned like the PR #1 docs test.
7. **Mother-app API (session+CSRF; extension token 401).** `POST …/audit`
   → 202 (dedupe in-flight), history/status/findings/evidence/cancel/retry.
   AC: auth-boundary tests, 202-without-execution test, duplicate-submit test.
8. **Mother-app UI.** AI Audit section in ListingDetail: pre/during/after
   states, polling, cancel, evidence + crop/mask inspection, exact blockers,
   empty/failure states. Never "AI verified" for visual support. AC:
   component tests + build.
9. **Evals.** `cases/v1/` covering the brief's 15 categories; runner writing
   `results/`; mock-vs-live separation; baseline documented first. AC:
   deterministic mock suite green; live suite reported blocked-or-executed,
   never faked.
10. **Docs.** ADR for any API correction; architecture/security/api-contracts
    updates; evals README status.

## 3. What PR #2 will not do

No live calls from import/startup/tests/CI/dashboard (zero-inference tests
stay green); no Redis/Celery/infra; no reviewer-correction UI, approval
history, or discrepancy export (data model ready for them); no model
substitution; no paid call without James's explicit approval.

## 4. Risks / open questions for James

1. **Approved 2026-10-31:** 6-call live smoke test (3 Muse + 3 SAM on
   synthetic fixtures, $0.50 hard cap). Keys not yet in `backend/.env`
   (file absent) — smoke runs once James adds them. Gated test:
   `backend/tests/test_live_smoke.py` (`VCC_LIVE_SMOKE=1` + keys, else
   skip; never in CI).
2. Brief §16 was truncated in transmission (mother-app tests cut off; no
   extension/E2E/delivery sections visible). Assumed: extension untouched
   except as needed, Playwright E2E extended, draft PR #2, CI green, no
   merge without your approval. Confirm?
3. Proposed `httpx` dependency (see §1). Veto?
4. Eval photos: default is extending `fixtures/generate.py` with ADAS-relevant
   synthetic scenes. Veto?
