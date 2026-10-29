# API contracts (v1)

Base URL: `http://127.0.0.1:8000`. Mother-app calls go through the Vite proxy
as relative `/api/*` paths. All JSON unless noted. Auth failures return
`401` (missing/invalid credential) or `403` (bad CSRF/setup token).

## Public (unauthenticated)

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/health` | `{status, service, api_version}`. No sensitive data. |
| GET | `/api/csrf` | `{csrf_token}`; also sets the CSRF cookie. |
| GET | `/api/setup/status` | `{initialized}` — safe to expose pre-auth. |
| POST | `/api/setup/initialize` | `{setup_token}` + CSRF header → opens session, returns `{pairing_id, extension_token}` **once**. `403` on bad token. |

## Mother app (session cookie + `X-CSRFToken` on writes)

| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/setup/logout` | Flushes the session. |
| GET | `/api/listings` | Paginated `{items, count}` (newest first). |
| POST | `/api/listings` | Manual create; same validation as extension. `201`. |
| GET | `/api/listings/{id}` | Listing + counts + status. `404` unknown. |
| GET | `/api/listings/{id}/photos` | Photo metadata for the gallery. |
| POST | `/api/listings/{id}/photos` | Manual photo upload (multipart, one file). `201`. |
| GET | `/api/listings/{id}/photos/{photo_id}` | Original bytes (`image/*`). IDs only — no paths. `404`/`410`. |
| GET | `/api/pairing/status` | Pairing metadata (never tokens/verifiers). |
| POST | `/api/pairing/rotate` | Revokes all active, returns new `{pairing_id, extension_token}` once. |
| POST | `/api/pairing/{id}/revoke` | Revokes one pairing. `404` if already revoked. |

The extension token is **not** accepted on any of these (`401`).

## Extension (`X-Extension-Token` header; no cookies, no CSRF)

| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/extension/listings` | Create listing. `201` with real DB id. `422` malformed. |
| POST | `/api/extension/listings/{id}/photos` | Multipart `file` (+ optional `source_url` form field). One photo per request. `201` with `{id, sha256, duplicate}`; `duplicate: true` on idempotent retry. `413` oversize, `422` invalid image, `404` unknown listing. |
| POST | `/api/extension/listings/{id}/failed-photos` | Report a photo that never reached the upload endpoint: `{source_url?, key?, kind: download\|permission\|validation\|upload, detail?}`. Idempotent per `(listing, key)` — repeats return `201` without double-counting. `422` on unknown kind or missing identity. |

Missing/invalid/revoked tokens return `401` and persist nothing.

The mother-app session boundary additionally exposes
`GET /api/listings/{id}/failed-photos`: all per-photo failure outcomes for
the listing (resolved included, so recovery stays visible).

## Import run states

The extension tracks `active` → `partial` / `complete` / `failed`: an active
run resumes after worker suspension (same listing id, stored photos skipped);
a finished run never auto-resumes — recovery is an explicit
`vcc:retry-failed` operation that reuses the original listing id and retries
only failed photos. A fresh import intent after a finished run creates a new
listing.

## Listing payload

```json
{
  "source_url": "https://… (http/https, ≤2048 chars)",
  "source_site": "example.test",
  "year": 1987,
  "make": "Test", "model": "Coupe", "trim": "LX",
  "vin": null,
  "title": "… (required)",
  "description": "… (≤50000 chars)",
  "advertised_claims": ["cruise control"],
  "expected_photo_count": 3
}
```

`vin` is optional; when present it must be 17 chars (no I/O/Q) and is
upper-cased. `advertised_claims`: ≤200 items, ≤500 chars each.

## Photo upload rules

JPEG/PNG/WebP only (verified by decoding, not by extension/MIME); ≤15 MiB
compressed; ≤40 MP decoded (header check before full decode;
decompression-bomb warnings are errors); ≤40 photos and ≤300 MiB per
listing. Idempotency is checked before capacity: re-uploading stored bytes
at a full listing returns the original record. The declared upload size is
checked before reading and bodies stream under a hard cap. Validation and
capacity rejections of fully-read bytes record idempotent per-photo
failures; read-level rejections record nothing. Successful uploads resolve
matching failure rows. Unresolved failures flip the listing to `partial`;
meeting `expected_photo_count` with no outstanding failures yields
`complete`.

## Future (not in PR #1)

`POST /api/listings/{id}/audit` will enqueue a durable audit job and return
`202 Accepted`. It is intentionally absent here — no fake audit success.
