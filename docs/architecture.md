# Architecture

## Components

| Component | Stack | Runs at | Trust level |
|-----------|-------|---------|-------------|
| Mother app | Svelte 5, TS, Vite, Tailwind | `http://127.0.0.1:5173` | Operator UI; session-authenticated |
| Backend | Django, Django Ninja, SQLite, Pillow | `http://127.0.0.1:8000` | Enforces all auth + validation |
| Extension content script | WXT, TS (isolated world) | Listing pages | Untrusted page DOM in, JSON out |
| Extension background worker | WXT, TS (MV3 service worker) | Extension context | Holds pairing token, fetches bytes |
| Fixture server | Python stdlib | `http://127.0.0.1:8901` | Test-only listing pages |

## Request flows

**Extension import.** Content script extracts metadata + image URLs from the
page and returns JSON to the background worker (no tokens, no bytes cross
runtime messaging). The worker validates the message + sender tab host,
creates the listing (`POST /api/extension/listings`), then downloads each
photo (allowlisted URLs with per-hop redirect revalidation, 16 MiB stream
cap, 30 s timeout) and uploads it (`POST …/photos`, one multipart request
each, concurrency 3, whole-image retries). Download failures that never
reach the upload endpoint are reported explicitly
(`POST …/failed-photos`) against the failing run's own listing id (never
re-read from shared state, so overlapping runs cannot misattribute
failures). Progress checkpoints to `session` storage after
every photo with an `active` / `partial` / `complete` / `failed` run status:
an interrupted active run resumes under the same listing id, while a
finished partial/failed run recovers only through an explicit retry that
reuses the listing id and skips stored photos (server dedupes by
`(listing, sha256)`, idempotency checked before capacity). Single-active
policy for the local single-user architecture: a competing import for
another listing is rejected while a run is active (in-worker guard plus a
checkpoint-level guard); terminal states release the guard. A checkpoint
left `active` by a terminated worker is recovered explicitly, never by
timeout: the popup shows Resume (same listing, original id) or Abandon;
abandon transitions unattempted photos to failed without filing backend
failure records and without touching the listing or stored photos, so a
different listing can be imported afterward and the original run can still
be retried. Abandonment is refused while an operation is in flight; a
live run is cancelled through the cancellation flag, which reports
intentional cancellation as an upload-phase outcome, never a validation
failure. Idle cancel holds the same in-worker guard across the transition:
a missing or terminal checkpoint is a no-op success, while persistence
failures return an error that the popup surfaces instead of a false ok.

**Mother app reads/writes.** The browser talks to Vite, which proxies
`/api/*` to Django — same-origin, no CORS. Reads use the operator session
cookie; state-changing calls add `X-CSRFToken`. Manual import reuses the
same service layer as extension import. Photo bytes stream from an
authenticated endpoint that accepts IDs only, never paths.

**Setup (first run).** Backend prints a one-time setup token to its own
terminal. The operator pastes it into `#/setup`; the backend verifies the
hash, marks the install initialized, opens the session, and issues the first
extension pairing token (displayed once). Rotation revokes all active
pairings and issues one replacement.

## Storage

```
backend/runtime-data/          # gitignored
├── vcc.sqlite3 (+ -wal/-shm)  # metadata, relationships, statuses, verifiers
├── originals/<uuid>.<ext>     # immutable original bytes (backend-named)
├── derived/{thumbnails,crops,masks}/   # PR #2+ derivatives (never originals)
└── temp/                      # atomic-write staging
```

SQLite runs `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=5000`
(`OPTIONS.init_command` + idempotent `post_migrate` WAL hook; verified by
`backend/tests/test_sqlite.py` on a file-backed DB). Transactions stay short;
nothing holds a transaction across network I/O.

## Provider contracts (PR #2: live behind keys)

`backend/providers/` defines per-model adapters with exact identifiers
(`meta-models/muse-spark-open-1026` on Baseten, `sam-3.1` on Meta),
bounded retries, structured errors, pure payload builders, and a
per-provider usage ledger. Every adapter call reports `last_attempts`
and token usage; missing usage means unknown cost, never zero.
Without keys, `analyze()` raises `ProviderNotConfigured` and the audit
fails closed with that code — no silent skip.
`backend/tests/test_zero_inference.py` still runs the whole import flow
with sockets blocked and keys unset: import never calls providers.

## Audit engine (PR #2)

One listing → one audit job (`AuditJob`), processed stage by stage:

1. Local deterministic screening (claims + photo snapshot) → 2. Muse
   full-image analysis (one photo × ≤6 claims per call, 4096 output
   tokens) → 3. SAM 3.1 localization of PRESENT sightings → 4. Pillow
   box crops → 5. conditional Muse crop re-analysis (documented
   ambiguity only) → 6. deterministic reconciliation (five-state
   taxonomy: `verified`, `visually_supported`, `insufficient_evidence`,
   `not_visually_verifiable`, `contradicted`) → findings + evidence.

Key mechanics (all covered by `backend/tests/test_audits*.py`):

- **Standalone worker process**: `python backend/manage.py run_audit_worker`
  (or `./scripts/start-audit-worker.ps1`). The queue endpoint only
  persists the job (`202`) — no in-request threads. One process runs
  one job at a time; the atomic claim keeps concurrent workers from
  double-running a job.
- **Durable resume**: claims, photo IDs, and SHA-256 values are
  snapshotted at queue time; stages read the snapshot, never the live
  listing. Completed observations/crops survive worker restarts and
  are never re-requested (no duplicate paid calls). A killed worker's
  job is reclaimed via its stale heartbeat lease.
- **Pre-call budgets**: every provider call first reserves worst-case
  attempts + spend headroom (`VCC_AUDIT_RESERVE_PER_CALL`) through a
  conditional UPDATE; losers fail closed without pausing. After the
  call, actuals replace the hold. Local stages consume no model
  allowance. Unknown cost, call-cap, or spend-cap pauses the job with
  an exact code; `model_calls_made` counts model attempts only
  (`calls_made` counts ledger rows, local stages included).
- **No exactly-once billing claim**: a provider call that finished
  server-side microseconds before a crash may go unsettled; providers
  offer no idempotency keys here. The ledger records what the app can
  prove (attempts, usage, cost or explicit unknown).

## Trust boundaries

1. **Dealership content is untrusted**: page DOM, URLs, filenames, MIME
   types, image bytes. Validated at every layer (see `security.md`).
2. **Extension token ≠ operator session**: header credential for
   `/api/extension/*` only; session cookie + CSRF for mother-app routes.
   Neither is accepted on the other's endpoints (tested).
3. **Secrets stay server-side / in trusted contexts**: model keys in backend
   env only; pairing token in background/popup storage only, never content
   scripts or pages; verifiers (SHA-256) in SQLite, never plaintext.
   `storage.local` is additionally restricted to `TRUSTED_CONTEXTS` at
   worker startup, so untrusted content-script contexts cannot read the
   token area (verified by a real-browser access-level test).
