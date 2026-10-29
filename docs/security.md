# Security

Localhost runtime, public source: every control below assumes the attacker
can read this repository and can serve arbitrary listing pages.

## Network posture

- Django binds `127.0.0.1`; `ALLOWED_HOSTS` is loopback + `testserver`.
- No CORS middleware exists. The mother app is same-origin through the Vite
  proxy; the MV3 background worker uses host permissions, not CORS. There is
  no `chrome-extension://*` wildcard anywhere.
- Extension host permissions are explicitly
  `http://127.0.0.1:8000/*` (API) and `http://127.0.0.1:8901/*` (fixture
  server). No all-hosts wildcard; content scripts match the fixture origin
  only. Dealership/CDN origins join as narrow optional permissions later.

## Authentication

- **Extension** (`/api/extension/*`): 256-bit random token in
  `X-Extension-Token`, verified with `hmac.compare_digest` against SHA-256
  verifiers in SQLite. Survives restarts; supports revocation + rotation.
  Rejected when missing/invalid/revoked (`401`).
- **Mother app** (everything else under `/api`): local-operator session
  cookie from a one-time terminal-printed setup token. django-ninja views
  are middleware-CSRF-exempt by design, so `OperatorSessionAuth` enforces
  CSRF itself (`X-CSRFToken` on unsafe methods); the setup bootstrap does
  the same. The extension token is not accepted here and vice versa.
- No privileged operation is reachable from arbitrary webpages: setup needs
  the terminal token, pairing management needs the session, imports need the
  header token.

## Input handling

- Listing fields: URL scheme/host checks, length caps, VIN charset,
  claim-count caps; malformed payloads are `422` and persist nothing.
- Filenames are metadata only (`basename`, ≤255 chars); storage names are
  backend UUIDs. Download/serving paths are derived server-side — directory
  traversal has no input channel (tested).
- Image pipeline: compressed-size check → Pillow format allowlist →
  header-dimension pixel cap (40 MP) **before** full decode →
  decompression-bomb warnings as errors → `verify()` → reopen + `load()` →
  atomic write of original bytes. MIME/extension are never trusted.
- Extension image fetches: public http(s) only, plus the explicit
  `127.0.0.1:8901` fixture exception. Loopback, RFC 1918, link-local, and
  non-http(s) targets are refused before any request is made; downloads
  stream through a 16 MiB cap with a 30 s timeout.

## Secrets & public-repo hygiene

- Never committed: `.env`, `runtime-data/` (DB, originals, Django secret),
  pairing tokens/verifiers, model keys, build output. `.gitignore` covers
  all of these; CI runs a secret scan and ships no credentials (providers
  are mocked; adapters refuse live calls without keys).
- Setup/pairing tokens are displayed once in the operator's own terminal or
  browser and never logged. Status endpoints expose metadata only.
- Generated frontend/extension bundles are scanned for secrets before push
  (see PR checklist in `AGENTS.md`).

## Known residual risks (PR #1)

- Single-operator threat model: anyone with local file access can read
  `runtime-data/`. Full-disk access is out of scope for a localhost tool.
- `http://` loopback (no TLS) is acceptable for loopback-only traffic but
  must be revisited if the bind address ever changes.
- Rate limiting is minimal (bounded concurrency, size caps, timeouts);
  abuse-tolerant multi-user hardening is out of scope.
