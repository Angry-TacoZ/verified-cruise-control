# Changelog

## Unreleased (PR #1 — Project Foundation)

Public repo + working listing-import pipeline:

- Public `Angry-TacoZ/verified-cruise-control`, `main` default,
  `feat/001-project-foundation` branch; root `AGENTS.md` governance (Rules A–K).
- Django + Ninja backend on `127.0.0.1:8000`: listings, original-photo
  storage (SHA-256, idempotent retries), extension pairing (persisted,
  revocable, rotatable), operator setup session + CSRF, SQLite WAL/FULL/5s.
- WXT MV3 extension: synthetic-fixture extractor, validated runtime
  messaging, background multipart uploads with progress/retries, narrow host
  permissions, Svelte popup (pairing, import, progress, open-in-app).
- Svelte mother app on `127.0.0.1:5173` (Vite `/api` proxy): dashboard,
  listing detail + original-photo gallery, setup, pairing, manual import.
- Provider contracts for Baseten Muse Spark Open 1026 + Meta SAM 3.1 with
  live calls disabled; import suite runs with sockets blocked.
- 70+ backend tests, 12 mother-app tests, 29 extension tests, Playwright
  browser smoke; GitHub Actions CI with mocked providers + secret scan.
- Docs: README, architecture, API contracts, security, ADR-001, eval
  methodology, synthetic fixtures + manual E2E guide.

## Unreleased (PR #1 remediation — independent review findings)

- Extension `storage.local` restricted to `TRUSTED_CONTEXTS` at worker
  startup; token reads gate on the restriction (Finding 1, HIGH).
- Photo idempotency checked before count/byte capacity, so identical
  retries at a full listing return the original record (Finding 2).
- Upload reads bounded (declared size first, chunked hard cap) even when
  size metadata lies (Finding 3).
- Explicit retry-failed-photographs operation reusing the original listing
  id, with `active`/`partial`/`complete`/`failed` run states; popup retry
  button (Finding 4).
- Per-photo failure records (`download`/`permission`/`validation`/`upload`)
  with atomic counters, idempotent reports, and resolve-on-success; mother
  app shows outstanding and recovered failures (Finding 5).
- Full IPv4/IPv6 non-public ranges rejected, redirects revalidated per hop
  (max 3), no credentials on image fetches (Finding 6).
- 90+ backend tests, 16 mother-app tests, 59 extension tests; extension E2E
  covers full + partial imports, retry, and the storage restriction.

Validation: full `./scripts/test.ps1` gates green; zero Baseten/Meta calls;
no credentials in source, history, logs, or artifacts. See the PR
description for evidence and screenshots.
