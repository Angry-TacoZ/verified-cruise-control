# AGENTS.md — Verified Cruise Control

Binding engineering expectations for all coding-agent sessions and pull
requests in this repository. The repo is **PUBLIC**; localhost runtime does
not imply public API access.

Product: a multimodal automotive listing-audit tool. It compares advertised
advanced driver-assistance (ADAS) features against visual evidence from
listing photographs plus vehicle-specific documentation, and produces
reviewable evidence packets. AI results are **advisory evidence** for a human
reviewer — the system must never independently certify disputed features.

Stack (locked): Svelte 5 + TypeScript + Vite + Tailwind mother app, Django +
Django Ninja + SQLite + Pillow backend, WXT + Svelte + TypeScript MV3 browser
extension. AI inference uses only Meta-developed models: Muse Spark Open 1026
(via Baseten) and SAM 3.1 (via Meta Model API), server-side only.

## Rule A: Engineering workflow

All work follows **Understand → Inspect → Plan → Implement → Verify → Report**.

Before editing:

1. Inspect repository state (`git status`, branch, recent commits).
2. Read relevant docs (`docs/`, `evals/README.md`, this file).
3. Locate affected code and tests; identify dependent components and trust
   boundaries.
4. Define expected observable behavior and acceptance criteria.
5. Plan the smallest cohesive implementation.

Do not perform unrelated cleanup during a focused change. Do not rewrite
working code without understanding its dependencies. Record consequential
choices as architecture decision records under `docs/decisions/`.

## Rule B: Verification-first development

A successful code edit does not establish that the intended result works.

For meaningful changes:

1. Define expected behavior; add or update tests first where practical.
2. Run relevant tests; exercise actual application behavior.
3. Inspect visible outcomes (rendered UI, stored rows, files on disk).
4. Record failures and limitations; resolve regressions before review.

Required verification categories (run each applicable one, never claim an
unexecuted test passed):

- Formatting and linting (`ruff`, `eslint`/`prettier`)
- Static/type checking (`svelte-check`, `tsc`)
- Unit, integration, database-migration, API-contract, security-boundary tests
- Frontend production build, extension production build
- End-to-end workflow tests; Playwright where browser verification materially
  improves confidence
- Runtime/visual verification for UI changes: layout, error states, responsive
  behavior, basic keyboard accessibility

## Rule C: AI inference discipline

Application inference uses only the approved Meta-developed models:

- Baseten `meta-models/muse-spark-open-1026` (base
  `https://inference.baseten.co/v1`, credential `BASETEN_API_KEY`)
- Meta `sam-3.1` (base `https://api.meta.ai/v1`, credential `META_API_KEY`)

Rules:

- Never silently substitute another model; no OpenAI/Anthropic/Google models.
- All model calls are server-side behind independent provider adapters in
  `backend/providers/`; never expose keys to frontend or extension code.
- Validate model responses before reconciliation logic sees them.
- Every inference execution records (when available): provider, exact model id
  + version, prompt/instruction version, request params, input photo ids,
  status, validation result, latency, retries, token usage, estimated cost,
  error info, audit job id. Never log credentials or excess payload.
- Costs stay attributable to separate Baseten vs Meta credit pools.
- No inference during CI, startup, import, or fixture testing. Tests and CI
  use deterministic mocks. The app must work without model keys until real
  inference lands.

## Rule D: Evaluation is mandatory

Meaningful AI, extraction, reconciliation, ranking, or verification work
requires reproducible evaluation:

1. Document a baseline; define a fixed versioned dataset + success criteria.
2. Run identical cases against baseline and candidate.
3. Preserve raw evidence and outputs; record failures.
4. Compare results/regressions; document retained vs reverted changes.
5. Report actual model usage and cost.

Tracked metrics (as features land): ADAS claim-extraction accuracy,
unsupported-claim recall, contradiction precision, localization accuracy,
unsupported-inference rate, human-escalation rate, reviewer-correction rate,
API latency, cost per listing, malformed-input reliability.

Never fabricate benchmarks. Never edit eval cases to improve scores. Keep
benchmark fixtures synthetic or properly licensed for public distribution.

## Rule E: Five-state evidence taxonomy

Reconciliation preserves exactly these states:

- **Verified** — authoritative vehicle-specific evidence establishes it.
- **Visually supported** — photos give meaningful but non-authoritative support.
- **Contradicted** — reliable evidence conflicts with the advertised feature.
- **Insufficient evidence** — available evidence cannot resolve the claim.
- **Not visually verifiable** — confirmation needs another evidence form.

Rules: missing visible evidence is not automatically negative; a generic
cruise button does not establish adaptive cruise; lane-keeping ≠
lane-centering; camera/radar housings do not establish software capabilities;
year/trim alone do not establish optional equipment; buttons do not prove
functionality. Every finding cites supporting evidence. Never invent
confidence scores, citations, observations, or bounding boxes. Reviewer
decisions stay inspectable and reversible.

## Rule F: Planned multimodal pipeline

Approved future pipeline (later PRs; PR #1 builds contracts only):

1. Local deterministic image screening (validation, hash dedupe, dimensions,
   reliable placeholder/irrelevant filtering).
2. Muse Spark Open 1026 full-image analysis.
3. SAM 3.1 targeted object localization (masks/boxes; Pillow crops originals
   at high resolution — never treat segmentation alone as capability proof).
4. Conditional Muse re-analysis of uncertain crops with original-image context.
5. Deterministic reconciliation. 6. Human QA.

Every second-pass inference records whether it changed the finding and what
it cost.

## Rule G: Evidence preservation

Original photographs are immutable evidence:

- Persist original bytes to local filesystem storage; compute SHA-256.
- Never overwrite originals with processed images; keep thumbnails, crops,
  masks in a separate derived area.
- Preserve source URLs + import provenance; link every derivative to its
  source photo.
- Keep inspectable history for model outputs and reviewer corrections.
- Images live outside SQLite; the DB stores metadata, relationships,
  statuses, paths — never large blobs. Never fabricate or silently alter
  evidence.

## Rule H: Security boundaries

Treat all dealership listing content as untrusted input.

- Bind Django to `127.0.0.1` by default; no unrestricted CORS. Mother app
  talks to Django through the Vite dev proxy (`/api` → `127.0.0.1:8000`)
  using relative paths; Vite binds `127.0.0.1` with `strictPort: true`.
- Authenticate extension imports with a cryptographically random pairing
  credential sent in an `X-Extension-Token` header (or documented
  equivalent), verified against a securely stored verifier with constant-time
  comparison. Persist pairings across restarts; support revocation and
  rotation; never log credentials.
- Keep pairing credentials away from content scripts and webpages: content
  scripts send only JSON metadata + image URLs to the background worker,
  which holds the token in trusted extension storage and performs
  authenticated multipart uploads (one photo per request, bounded
  concurrency, progress in persistent extension storage, safe whole-image
  retries — never describe these as partial-byte resumable uploads).
  `storage.local` must be restricted to `TRUSTED_CONTEXTS` at worker
  startup and token reads must gate on that restriction; runtime messages
  never carry credentials.
- Mother-app operator auth is a separate boundary: a local-operator
  bootstrap/session mechanism, not the extension token. Never require the
  extension token on mother-app endpoints. Preserve host validation and CSRF
  defenses for cookie-authenticated state-changing requests; never expose
  privileged operations to arbitrary webpages just because the backend is
  localhost.
- Extension host permissions are narrowly scoped (local Django API, fixture
  server, specific site/CDN origins). No `<all_urls>` shortcut, no
  `chrome-extension://*` CORS wildcard. CORS never substitutes for token
  auth. Never route page-supplied URLs into privileged local/private-network
  fetches; validate schemes, origins, every redirect destination (bounded
  hop count), and response sizes. Image downloads never carry credentials.
  Hostnames are checked as written: document the DNS-resolution limitation
  rather than claiming post-resolution enforcement.
- Import failures are backend-visible per-photo records (`download` /
  `permission` / `validation` / `upload`), reported idempotently — never
  extension-only state. Failure counters use atomic updates, never
  in-memory values; successful retries resolve prior failures so status
  reflects current evidence. Upload reads are bounded (declared size first,
  then chunked with a hard cap). Idempotency is checked before capacity so
  identical retries at a full listing return the original record.
- Validate filenames, paths, image types, dimensions, sizes, URLs, and
  payloads. Prevent directory traversal and unrestricted SSRF of supplied
  image URLs. Apply request limits, timeouts, bounded retries.
- Pillow validation sequence: enforce compressed size limits before parsing;
  allowlist formats; open without full decode; reject width×height over the
  40 MP cap before decoding pixels; treat decompression-bomb warnings as
  errors; `Image.verify()` then reopen for further decode; preserve original
  bytes only after validation. `MAX_IMAGE_PIXELS` alone is not the cap.

Never weaken security controls to make tests pass.

## Rule I: Public repository hygiene

- Synthetic or properly licensed fixtures only; never commit runtime DBs,
  imported photos, `.env` files, API keys, pairing tokens/verifiers, Django
  secrets, session credentials, or machine-specific config.
- Review staged changes before every commit; scan source and generated
  frontend/extension bundles for secrets; keep secrets out of CI logs,
  screenshots, and PR descriptions.
- Document env vars without values; keep setup reproducible without private
  infrastructure; keep the demonstrated state traceable to commits.
- Never claim public source makes the localhost API publicly accessible.

## Rule J: Git and PR workflow

Inspect before modifying; scoped branches; coherent commits; no unrelated
changes; tests + docs alongside code; verify before review; preserve useful
failed experiments; reviewable PRs. **Never merge without James Lane's
explicit approval.**

## Rule K: Final reporting

Every completed task reports: what/why changed, files modified, tests run
(passed/failed), runtime verification, security checks, model API calls made,
actual/estimated cost, remaining risks, limitations, commit hash, PR URL when
available. Unsupported success claims are prohibited.

## Appendix: PR #1 acceptance checklist

- [ ] Repo PUBLIC under `Angry-TacoZ`, `main` default, feature branch exists
- [ ] Django Ninja backend + Svelte mother app start; migrations succeed; WAL
  verified on a fresh file-backed DB (`journal_mode=WAL`, `synchronous=FULL`,
  `busy_timeout=5000`)
- [ ] WXT extension builds and loads; secure pairing works and persists
  across backend restart
- [ ] Synthetic listing import moves real data + original photos end to end;
  originals on disk, metadata + hashes in SQLite; duplicates idempotent
- [ ] Mother app shows real imported listings/photos; manual import fallback
  works; no fabricated audit findings, scores, or "AI-verified" labels
- [ ] Auth/input-validation tests pass; frontend + backend checks green;
  E2E executed or limitations explicitly reported
- [ ] CI runs with mocked providers only; zero Baseten/Meta calls; no
  credentials in source, history, logs, or artifacts
- [ ] README enables public reproduction; no paid credits consumed
- [ ] Reviewable draft PR exists; PR #2 (live inference, audit worker,
  reconciliation, evals) explicitly out of scope

