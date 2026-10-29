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

Validation: full `./scripts/test.ps1` gates green; zero Baseten/Meta calls;
no credentials in source, history, logs, or artifacts. See the PR
description for evidence and screenshots.
