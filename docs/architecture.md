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
photo (allowlisted URLs, 16 MiB stream cap, 30 s timeout) and uploads it
(`POST …/photos`, one multipart request each, concurrency 3, whole-image
retries). Progress checkpoints to `session` storage after every photo, so a
suspended worker resumes without duplicating evidence (server dedupes by
`(listing, sha256)`).

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

## Provider contracts (PR #1: interfaces only)

`backend/providers/` defines per-model adapters with exact identifiers
(`meta-models/muse-spark-open-1026` on Baseten, `sam-3.1` on Meta),
timeout/retry policies, structured errors, pure payload builders, and a
per-provider usage ledger. `analyze()` raises `ProviderNotConfigured`;
`backend/tests/test_zero_inference.py` runs the whole import flow with
sockets blocked and keys unset. PR #2 implements live calls behind these
interfaces plus durable audit jobs:

1. Local deterministic screening → 2. Muse full-image analysis → 3. SAM 3.1
   localization → 4. conditional Muse crop re-analysis → 5. deterministic
   reconciliation (five-state taxonomy) → 6. human QA.

## Trust boundaries

1. **Dealership content is untrusted**: page DOM, URLs, filenames, MIME
   types, image bytes. Validated at every layer (see `security.md`).
2. **Extension token ≠ operator session**: header credential for
   `/api/extension/*` only; session cookie + CSRF for mother-app routes.
   Neither is accepted on the other's endpoints (tested).
3. **Secrets stay server-side / in trusted contexts**: model keys in backend
   env only; pairing token in background/popup storage only, never content
   scripts or pages; verifiers (SHA-256) in SQLite, never plaintext.
