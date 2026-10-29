# Verified Cruise Control

A multimodal automotive listing-audit tool: it compares advertised
advanced driver-assistance (ADAS) features against visual evidence from
listing photographs plus vehicle-specific documentation, and produces
reviewable evidence packets for a human reviewer.

> **Hackathon project — public by requirement.** This repository is
> intentionally public. All fixtures are synthetic or generated in-repo.
> AI results are advisory evidence for a human reviewer; the system never
> independently certifies disputed features.

**PR #1 scope (this branch):** project foundation + listing import —
`Synthetic Listing → WXT Extension → Secure Django API → SQLite + Original
Image Storage → Svelte Mother App`. Live AI inference ships in PR #2; PR #1
defines the provider contracts but makes zero model calls.

## Architecture

```
┌──────────────┐  runtime msgs   ┌───────────────────┐  HTTPS-less loopback   ┌────────────────┐
│ Listing page │ ── JSON only ──▶ │ Extension (MV3)   │ ── X-Extension-Token ─▶ │ Django + Ninja │
│ + content    │                 │ background worker │    multipart uploads   │ SQLite + files │
│   script     │                 │ (holds token,     │                        │ 127.0.0.1:8000 │
└──────────────┘                 │  fetches bytes)   │                        └───────┬────────┘
                                 └───────────────────┘                                │ Vite proxy
┌──────────────┐  session+CSRF   ┌───────────────────┐  relative /api paths   ┌───────▼────────┐
│ Operator     │ ◀────────────── │ Svelte mother app │ ◀────────────────────── │ 127.0.0.1:5173 │
│ (browser)    │                 │ dashboard         │                        └────────────────┘
└──────────────┘                 └───────────────────┘
```

- **Mother app** (`apps/mother`): Svelte 5 + TypeScript + Vite + Tailwind.
  Dashboard, listing detail + photo gallery, operator setup, extension
  pairing, manual import fallback.
- **Backend** (`backend/`): Django + Django Ninja + SQLite (WAL) + Pillow.
  Listing/photo/pairing models, two auth boundaries (extension token vs
  operator session + CSRF), provider contracts (no live calls in PR #1).
- **Extension** (`apps/extension`): WXT + Svelte + TypeScript (MV3).
  Content script extracts metadata → background worker downloads photos and
  uploads them one-per-request with progress + retries.
- **Docs**: [architecture](docs/architecture.md) ·
  [API contracts](docs/api-contracts.md) · [security](docs/security.md) ·
  [stack decision](docs/decisions/001-stack.md) · [evals](evals/README.md)

## Prerequisites

- Windows 10/11 with PowerShell (primary dev environment), macOS/Linux also work
- Python 3.13+ and Node.js 20+
- Chrome/Chromium for the extension demo

No cloud account, API keys, or paid services are needed for PR #1.

## Quickstart (Windows PowerShell, from repo root)

```powershell
./scripts/setup.ps1        # once: venv, deps, migrations, npm installs
./scripts/dev.ps1          # backend :8000 + mother app :5173 + fixtures :8901
```

Then:

1. Open `http://127.0.0.1:5173` — copy the one-time setup token from the
   backend window into `#/setup`. This signs in the browser and issues the
   extension pairing token (shown once).
2. Build/load the extension: `npm run build --prefix apps/extension`, then
   `chrome://extensions` → Developer mode → **Load unpacked** →
   `apps/extension/.output/chrome-mv3`. Paste the pairing token in the popup.
3. Open `http://127.0.0.1:8901/listing-1987-coupe.html`, click the extension
   icon → **Import this listing**.
4. Open the listing in the mother app — real rows in SQLite, original bytes
   on disk under `backend/runtime-data/` (gitignored).
5. `./scripts/stop-dev.ps1` stops everything.

If a port is occupied, prefer stopping the conflicting process — ports are
fixed (`8000`/`5173`/`8901`) so the extension manifest and proxy agree.

### Port conflicts

When another project owns a port, override without editing code:

| Component | Variable | Default | Notes |
|-----------|----------|---------|-------|
| Backend | (CLI arg) | `8000` | `python backend/manage.py runserver 127.0.0.1:8001` |
| Mother proxy target | `VCC_API_PROXY` | `http://127.0.0.1:8000` | Point at the moved backend |
| Mother dev port | (CLI arg) | `5173` | `npx vite --host 127.0.0.1 --port 5174 --strictPort` in `apps/mother` |
| Extension API base | `VCC_API_HOST` + `VITE_VCC_API_BASE` | `http://127.0.0.1:8000` | Rebuild the extension after setting both |
| Fixture server | (CLI arg) | `8901` | Keep `8901` for E2E: the extension content-script matches and extractor are pinned to `127.0.0.1:8901` (see `listing.content.ts`, `extractors.ts`) |

E2E runs accept `VCC_E2E_API_PORT` / `VCC_E2E_WEB_PORT` / `VCC_E2E_FIX_PORT`
(the extension artifact must be rebuilt with matching overrides).

## Reproduce the demo end to end

```powershell
./scripts/test.ps1          # full gates: backend + mother + extension
npx playwright test --prefix apps/mother   # browser smoke (after npm i)
```

Manual extension E2E (also in `fixtures/README.md`): import the synthetic
listing, verify photos/hashes/statuses in the mother app, restart Django,
verify pairing + evidence persist, re-import and confirm no duplicate
evidence. The partial fixture (`listing-partial.html`) exercises the
failed-photo path: one photo 404s, the failure is recorded server-side and
shown in the mother app, and the popup's retry reuses the same listing.
Zero Baseten/Meta calls by construction (adapters refuse in PR #1).

## Repository layout

```
apps/mother/        Svelte mother app (Vite proxy /api → Django)
apps/extension/     WXT browser extension (MV3, Chrome/Chromium)
backend/            Django project: config/ listings/ evidence/ pairing/ providers/
docs/               architecture, api-contracts, security, decisions/
fixtures/           synthetic listings + generated photos + fixture server
evals/              evaluation methodology (results land with PR #2)
scripts/            Windows-first dev scripts (setup/dev/test/stop)
```

Runtime state (`backend/runtime-data/`: SQLite, originals, secrets) is never
committed. Copy `backend/.env.example` to `backend/.env` for local keys.

## Tests

| Area      | Command (repo root)                              | Coverage |
|-----------|--------------------------------------------------|----------|
| Backend   | `.venv/Scripts/python -m pytest backend/`        | 90+ tests: migrations, WAL, import, photos, failure records, concurrency, pairing, CSRF, zero-inference |
| Mother    | `npm test --prefix apps/mother`                  | API client, dashboard, import form, failure display |
| Extension | `npm test --prefix apps/extension`               | Extractor, protocol, uploader, retry, URL/redirect policy, storage guards, popup UI |
| Browser   | `npx playwright test` in `apps/mother`           | Setup → dashboard → manual import smoke + extension E2E (full + partial import, retry, storage restriction) + screenshots |

CI (`.github/workflows/ci.yml`) runs lint, typecheck, tests, and production
builds on every PR with mocked providers only — no model credentials exist
in CI.

## Known limitations (PR #1)

- No live AI inference: no Muse/SAM calls, no audit jobs, no findings UI.
  The mother app deliberately shows “imported, not audited” everywhere.
- One extractor (synthetic fixture server); dealership rules are PR #2+.
- Photo gallery enumerates stored photos; crops/masks/thumbnails unenforced.
- Single local operator, single pairing token family, no multi-user auth.
- SQLite + local disk only; no backups, no replication.

## Roadmap

- **PR #2**: live Muse Spark Open 1026 + SAM 3.1 integration, local audit
  worker, durable jobs, Pillow crops, deterministic reconciliation,
  five-state findings, baseline evals with real cost accounting.
- Later: dealership extractor pack, reviewer QA workflow, evidence packets,
  packaging hardening.

## Fixture provenance & license

All images and listings under `fixtures/` are generated in-repo (`fixtures/generate.py`)
and watermarked SYNTHETIC. No real dealership data, no stock photos, no
tracking pixels. Licensed for public hackathon distribution — see
[CHANGELOG](CHANGELOG.md) for PR history.
