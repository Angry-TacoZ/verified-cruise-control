# ADR 001: Locked stack for Verified Cruise Control

- Status: accepted
- Date: 2026-10-29
- Context: PR #1 foundation; choices below are binding for later PRs unless a
  new ADR supersedes them.

## Decision

| Area | Choice | Why |
|------|--------|-----|
| Mother app | Svelte 5 + TypeScript + Vite + Tailwind | Small reactive UI, Svelte 5 runes, Vite proxy removes CORS; shared language with the extension |
| Backend | Django + Django Ninja + SQLite + Pillow | Batteries-included ORM/migrations/sessions; Ninja gives typed contracts; SQLite WAL is enough for localhost evidence; Pillow validates + crops originals |
| Extension | WXT + Svelte + TS, MV3, Chromium-first | WXT handles MV3 build/manifest/storage with sane defaults; Svelte popup shares components with the mother app |
| Vision LLM | Baseten `meta-models/muse-spark-open-1026` | Required Meta model; OpenAI-compatible `/v1` base keeps the adapter simple; $100 promo credits |
| Segmentation | Meta `sam-3.1` via Meta Model API | Required Meta model for targeted localization; separate credit pool, separate adapter |
| Eval/fixtures | In-repo synthetic data + pytest/vitest/Playwright | Public-safe, reproducible, zero-cost; no private records |

All AI calls are server-side behind independent provider adapters with
separate usage ledgers. PR #1 ships the contracts with live calls disabled.

## Alternatives considered

- **Electron/Tauri**: rejected — a browser + localhost server covers the
  demo without native packaging.
- **PostgreSQL/Redis/Celery**: rejected for PR #1 — SQLite WAL + a local
  worker process (PR #2) is sufficient and keeps setup to one command.
- **Non-Meta models**: rejected — hackathon constraint is Meta-only
  inference; silently substituting would violate Rule C.
- **Plasmo / raw MV3**: rejected — WXT's entrypoint/storage conventions
  plus first-class Svelte support fit better.
- **React/Vue**: rejected — Svelte 5 runes give the smallest bundle and the
  team already standardizes on it across both frontends.

## Consequences

- Windows-first scripts (PowerShell) with cross-platform fallbacks.
- Pinned versions + committed lockfiles (`requirements*.lock`,
  `package-lock.json`) for reproducible public builds.
- Provider API contracts must be re-verified against live docs before PR #2
  wires real inference (bases/keys are placeholders until then).
