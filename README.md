# Verified Cruise Control

A multimodal automotive listing-audit tool: it compares advertised
advanced driver-assistance (ADAS) features against visual evidence from
listing photographs plus vehicle-specific documentation, and produces
reviewable evidence packets for a human reviewer.

> **Hackathon project — public by requirement.** This repository is
> intentionally public. All fixtures are synthetic or generated in-repo.
> AI results are advisory evidence for a human reviewer; the system never
> independently certifies disputed features.

**Current scope (PR #6, this branch):** foundation + listing import
(PR #1) and the multimodal audit engine — Muse visual observations, SAM
localization, manufacturer-aware interpretation, deterministic five-state
reconciliation, durable worker, enforced spend caps (PR #2) — plus an
offline-first paired harness comparing baseline (m2) vs
reference-assisted (m3) recognition on real-world photos
(`evals/realworld/`, `docs/realworld-evaluation.md`), cross-brand
evidence generalization (PR #4), and the site-neutral real-listing ACC
path: a user-activated generic extractor that works on unfamiliar
dealership sites without per-site code (Autotrader kept as a precision
rule), operator review/correction of the detected vehicle and photos,
operator-requested ACC checks kept separate from seller claims,
photo-gated audit queueing, and a PASS / INCONCLUSIVE / FAIL
presentation layer over the five-state taxonomy. Reference-assisted
recognition is experimental and unvalidated against live inference; no
live model comparison has been authorized.

## Install and Run on Windows

No terminal needed for everyday use. Three double-clickable files in the
repo root do everything; the app itself runs locally on your PC.

**One-time installation.** Double-click `Install Verified Cruise
Control.cmd`. It checks for Python 3.13+ and Node.js 20+ (with download
links if either is missing), installs what is missing, prepares the
database, and builds the Chrome extension. Re-running it later is safe:
it only fills gaps and never deletes anything.

**Load the extension into Chrome (once).** The installer prints the exact
extension folder (`apps/extension/.output/chrome-mv3` under the repo)
and offers to open it. Then: visit `chrome://extensions`, enable
Developer mode, choose Load unpacked, select that folder, and pin
Verified Cruise Control to the toolbar. The installed extension stays
put across restarts; rebuilding later only replaces its files and never
clears pairing or permissions.

**Daily use.** Double-click `Start Verified Cruise Control.cmd`. It
verifies setup, starts the backend, the audit worker, and the local app
(exactly once each -- a second double-click just reuses what is already
running), waits until everything answers, then opens the dashboard in
your browser. The synthetic photo server developers use is not needed
for real listings and is never started by the launcher.

**First run only: pairing.** The launcher shows a one-time operator
token. Enter it on the mother-app setup page, copy the extension pairing
token shown there into the extension popup in Chrome, and you are done --
later starts skip pairing entirely unless credentials are revoked.

**Analyze a vehicle listing.** Open any individual vehicle listing page,
click the extension, choose Review listing, confirm or correct the
vehicle and photos (attach your own images if the site blocks downloads
-- they are labelled user-supplied), then Verify Adaptive Cruise
Control. Back in the mother app, open the listing and click Run ACC
Audit, then inspect the result and its evidence.

**Inference costs nothing by default.** The launcher starts with paid
inference OFF and says so on every start. Audits you queue will wait
with an "Inference is disabled" note until you deliberately enable it
(see `backend/.env.example`) and authorize each run yourself. A green
startup report means the local app works -- it does NOT mean live model
calls work.

**Stopping.** Double-click `Stop Verified Cruise Control.cmd`. It stops
only the services the launcher started (tracked by process id and
verified before stopping) and cleans up. Unrelated Python, Node, or
browser processes are never touched. If you also ran the developer
scripts, stop those separately with `./scripts/stop-dev.ps1`.

**Updating the extension after code changes.** Run
`./scripts/build-extension.ps1`, then press the reload button for the
extension in `chrome://extensions`. No re-pairing needed.

**Troubleshooting.** If a port (default 8000 for the backend, 5173 for
the app) is already in use, the launcher names the port and the owning
program and stops -- it never kills anything. Free the port or close the
other program and start again. Service logs live under
`%LOCALAPPDATA%\VerifiedCruiseControl\logs`. If the launcher reports
missing setup pieces, re-run the installer. Windows integration coverage
for the launchers lives in `scripts/test-launcher.ps1`.

## Why this is more than an image describer (hackathon context)

**The real-world problem.** Used-car listings routinely advertise driver-assistance
features — adaptive cruise control, lane-keeping, blind-spot monitoring — that
the photos may or may not support. Checking a listing means testing each
advertised claim against the visual evidence, not admiring one photo.

**Why several photographs matter.** One photo rarely settles a claim: the
steering-wheel buttons may be visible in an interior shot while the radar sensor
is only checkable in a front-grille close-up. Verification tests every
advertised claim against *each* relevant photo and records what each photo does
and does not show.

**How Meta models do substantive work here (shipped in PR #2).** PR #1 made
zero model calls by design — it built the evidence pipeline the models now
consume:

- **Muse Spark Open 1026** (Baseten) provides visual understanding of every
  listing photo: which assistance controls, sensors, and indicators are visible,
  and where.
- **SAM 3.1** (Meta) localizes and segments the named parts — the steering-wheel
  button cluster, the grille sensor window — so verdicts rest on precise regions
  instead of vague whole-image captions.
- Uncertain regions trigger **conditional crop re-analysis**: Muse looks again
  at the zoomed crop before anything is recorded.
- **Deterministic reconciliation** (rules, not model judgment) compares the
  per-photo observations against the advertised claims and assigns each claim
  one of five evidence states.
- **Source evidence is preserved**: original bytes are immutable and
  SHA-256-verified, so every verdict links back to the exact pixels a reviewer
  can re-open.
- The output is a **discrepancy report** — claim-by-claim states with supporting
  crops — built for a human reviewer. The system never certifies a disputed
  feature on its own.

| | Simple image analyzer | Verified Cruise Control |
|---|---|---|
| Input | one image + one prompt | whole listing: all photos + advertised claims |
| Model role | caption the picture | per-photo observation, then localized re-checks |
| Claim linkage | none | every verdict tied to a specific advertised claim |
| Localization | none | SAM segmentation + crop re-analysis |
| Conflicting photos | last answer wins | deterministic reconciliation per claim |
| Output | a description | discrepancy report with five evidence states |
| Human review | n/a | required: reports are advisory evidence |

**Concrete illustrative example (intended multi-photo reasoning, not a
live-model transcript).** Suppose a listing claims *adaptive cruise
control*. Photo A (interior) shows steering-wheel distance-control buttons
— Muse would observe them and SAM would segment the button cluster: that
sub-check reads *visually supported*, never *verified* (buttons alone do
not prove the feature works). Photo B (front grille) is too low-resolution
to confirm or deny the radar sensor: *insufficient evidence*, not a
contradiction — a photo's failure to show equipment never proves absence.
Reconciliation keeps the claim open and asks for a closer grille photo
instead of guessing — a single-image caption would have answered from one
photo and stopped.

What is actually demonstrated: the scripted Chromium E2E
(`e2e/acc.spec.ts`, fake adapters, $0) imports the synthetic listing,
queues an ACC audit through the authorized workflow, and shows the
resulting PASS finding in the mother app; see
`docs/demo/acc-dashboard.png` and `docs/demo/acc-detail.png`
(regenerated by that spec). The named Muse observations and SAM masks in
the narrative above illustrate how the PR #2 pipeline reasons across
photos — they were not produced by live model calls in that run, and no
live Muse/SAM comparison has been authorized.

**The five evidence states (approved taxonomy, AGENTS.md Rule E).** Each
advertised claim ends in exactly one state: **Verified** (authoritative
vehicle-specific evidence establishes the feature), **Visually supported**
(photos give meaningful but non-authoritative support), **Contradicted**
(reliable evidence conflicts with the advertised feature),
**Insufficient evidence** (available evidence cannot resolve the claim), or
**Not visually verifiable** (confirmation needs another evidence form). PR #1
stores the photos and claims these states will be computed from.

**Status honesty.** Shipped: listing capture, immutable original-photo
preservation, pairing, local storage, dashboard, and tests with zero paid
model calls (PR #1); live Meta inference, multimodal analysis,
conditional rechecks, deterministic reconciliation, initial evals (PR #2);
the site-neutral real-listing ACC path — generic user-activated
extraction with operator review, operator-requested checks, photo-gated
queueing, PASS / INCONCLUSIVE / FAIL display (PR #6, this branch).
Later: full human review workflows and discrepancy-report export. Per
reported hackathon guidance (unverifiable against a published source —
treat as reported, not official), the Simple Image Analyzers exclusion
targets generic one-prompt projects, and applications reconciling listing
claims across several images into reviewable discrepancy reports fit the
intended larger-workflow pattern; that is guidance, not a guarantee of
acceptance.

**Hackathon theme fit — Build for Your World.** Meta's models are brought
into the used-car domain: Muse Spark Open 1026 reads assistance controls
and indicators in listing photos, SAM 3.1 localizes the exact regions,
and deterministic reconciliation turns both into per-claim verdicts. The
firsthand problem is verifying advertised driver-assistance features
against the photos that are supposed to show them — work that today means
manual photo-by-photo inspection. The tool frees up that time by
compiling every claim, photo, observation, crop, and cost into one
reviewable evidence packet for a human reviewer, making claim-linked,
localized discrepancy reports possible where a generic caption could
only describe one picture.

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
./scripts/dev.ps1          # backend :8000 + audit worker + mother app :5173 + fixtures :8901
```

Then:

1. Open `http://127.0.0.1:5173` — copy the one-time setup token from the
   backend window into `#/setup`. This signs in the browser and issues the
   extension pairing token (shown once).
2. Build/load the extension: `npm run build --prefix apps/extension`, then
   `chrome://extensions` → Developer mode → **Load unpacked** →
   `apps/extension/.output/chrome-mv3`. Paste the pairing token in the popup.
3. Open `http://127.0.0.1:8901/listing-1987-coupe.html` (or any vehicle
   listing page), click the extension icon → **Review listing**, confirm or
   correct the detected vehicle and photos (attach your own images if the
   site blocks downloads — they are labelled user-supplied), then
   **Verify Adaptive Cruise Control** (imports the listing and marks ACC
   as the operator-requested evaluation target; plain **Import photos**
   skips the requested check).
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
Import makes zero Baseten/Meta calls by construction.

## Audit engine (PR #2)

Audits compare advertised claims against the imported photos and produce
per-claim findings in five states (`verified`, `visually_supported`,
`insufficient_evidence`, `not_visually_verifiable`, `contradicted`).

Run one (needs `VCC_INFERENCE_ENABLED=1` plus `BASETEN_API_KEY` and
`META_API_KEY` in `backend/.env` — see `backend/.env.example`):

1. `./scripts/dev.ps1` (the audit worker starts in its own window), or
   `./scripts/start-audit-worker.ps1` standalone.
2. In the mother app, open a listing → **Run audit**. This only queues
   the job (`202`); the worker claims it and runs screening (incl.
   SHA-256 integrity) → Muse → SAM → crops → conditional recheck →
   reconcile.
3. Findings, observations, crops, and the cost ledger appear in the
   audit panel; `/api/audits/{job_id}/evidence` exposes the same data.

Muse names physical controls (button, symbol, location); the
versioned ADAS reference catalog (`docs/adas-catalog.md`) maps those
to documented vehicle capabilities, and manufacturer-specific entries
apply only to vehicles inside their documented make/model/year range;
SAM localizes the claim-relevant object — never an abstract feature;
deterministic reconcile assigns the final state plus an inspectable
interpretation (observed object, reference, strength, cannot-infer
list, per-control provenance). Dedicated controls (e.g. a following-
distance button) can yield `visually_supported`; generic ones never
promote upward.

Spend controls (see `docs/security.md`): every provider call reserves
worst-case attempts + spend before firing; per-job call and spend caps
pause with exact codes; unknown billing is never booked as zero; local
stages consume no model allowance. Killing the worker mid-audit is
safe — the next worker pass reclaims the job and resumes without
re-firing completed calls (exactly-once billing is not claimed).

### Real-listing ACC checks (PR #6, site-neutral)

- **User-activated generic extraction** (see
  `docs/decisions/003-site-neutral-extraction.md`): the popup inspects
  the active tab via `scripting` + `activeTab` — no content scripts run
  automatically on listing sites, no listing host is pre-granted, and no
  `<all_urls>`. Structured data (JSON-LD `Car`/`Vehicle`/`Product`,
  OpenGraph) first, generic DOM gallery analysis second, scored heuristic
  interpretation with per-field provenance third. Site rules
  (`autotrader-v1`, see `docs/decisions/002-real-listing-site.md`)
  survive as precision improvements; the generic extractor is the
  default fallback, so unfamiliar dealership sites work without a new
  deployment. Search, index, and ad pages are rejected, never mistaken
  for listings.
- **Operator review before import**: the popup shows detection
  confidence, seller claims (shown, never edited), editable vehicle
  fields (corrections marked operator-entered), a photo checklist, and
  per-origin host grants requested at runtime behind an explicit action.
  Uncertain galleries fall back to photo selection; undetected pages
  fall back to manual entry linked to the page URL. Unpermitted image
  hosts surface as recorded permission failures, never silent drops.
- **User-supplied photos** (operator-attached local files) are stored
  with `user_supplied` provenance and no page URL — never attributed to
  the listing page. A completed audit with no ACC finding reports
  **ACC finding unavailable**, never an evidence-based result.
- **Requested checks stay separate**: the operator's ACC selection travels
  the extension protocol on an allowlist and is stored as
  `requested_checks` — never merged into `advertised_claims`. A
  search-filter selection never becomes a seller claim.
- **Photo-gated queueing**: audits refuse photo-less listings (HTTP 422)
  before any provider spend; a failed or empty import can never trigger
  paid inference.
- **Presentation**: the five-state taxonomy is preserved internally and
  shown as PASS (`verified`/`visually_supported`), FAIL (`contradicted`),
  or INCONCLUSIVE (everything else); queued/running/paused/failed audits
  display as operational states, never as evidence results. The dashboard
  shows the latest ACC result per listing; the detail page shows
  provenance, the banner, findings, observations, crops, and costs.
- **Limits**: generic extraction is heuristic — low-confidence pages need
  operator review, bot-walled or gallery-blocked sites need the manual
  fallbacks, and no claim is made that every vehicle website works.

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
| Backend   | `.venv/Scripts/python -m pytest backend/`        | 492 collected (482 passed, 10 skipped): migrations, WAL, import, photos (incl. user-supplied provenance), failure records, concurrency, pairing, CSRF, zero-inference, audits (queue/claim/budget/stages/worker), providers, eval goldens, requested checks, photo-gated queueing |
| Mother    | `npm test --prefix apps/mother`                  | API client, dashboard, import form, failure display, audit panel, ACC verdict mapping (incl. finding-unavailable), dashboard badges, provenance display |
| Extension | `npm test --prefix apps/extension`               | Generic + site extractors (7 synthetic-DOM fixture cases), protocol (requested-check + review payloads), uploader (overrides/manual/user files), scripting injection, host permissions, URL/redirect policy, storage guards, popup review UI |
| Browser   | `npx playwright test` in `apps/mother`           | Setup → dashboard → manual import smoke + extension E2E (full + partial import, retry, storage restriction) + Verify-ACC import → queued audit → settled PASS finding (`acc.spec.ts`, scripted adapters, $0) + screenshots |

CI (`.github/workflows/ci.yml`) runs lint, typecheck, tests, and production
builds on every PR with mocked providers only — no model credentials exist
in CI.

## Known limitations (PR #6 branch)

- Live inference spends real credits: keep `VCC_INFERENCE_ENABLED` off
  unless you mean it, and never set `VCC_LIVE_SMOKE=1` in CI.
- The generic extractor is verified against 7 synthetic-DOM fixture
  cases (structured, dynamic, incomplete, ad-cluttered, non-listing,
  cross-origin CDN, blocked gallery), not live pages: headless
  automation is bot-walled on real marketplaces, so genuine two-site
  verification needs the operator's own headed Chrome session (checklist:
  `docs/demo/two-site-checklist.md`). Unrecognized or blocked pages fall
  back to operator photo selection and manual entry.
- Reconciliation is conservative by design: photo-only audits reach
  `contradicted`/`verified` only with authoritative backing, so most
  real listings land in `insufficient_evidence` until more sources exist.
  The v2 ADAS catalog keeps the Kia Forte and VW Jetta/GLI mappings
  and adds general cross-manufacturer patterns (dedicated LKA/LDW and
  ACC-distance controls, centering controls, assistance menus) that
  apply to any brand, capped at `visually_supported` and labeled for
  reviewer confirmation (see docs/adas-catalog.md). Recognition
  accuracy on real photographs is still unmeasured, and the m4 pattern
  prompt ships OFF for that reason.
- Single local operator, single pairing token family, no multi-user auth.
- SQLite + local disk only; no backups, no replication.

## Roadmap

- **PR #2 (merged)**: live Muse Spark Open 1026 + SAM 3.1
  integration, standalone audit worker, ownership-fenced durable jobs,
  SHA-verified evidence, Pillow crops, manufacturer-aware ADAS
  evidence mapping, deterministic reconciliation, five-state findings,
  baseline evals with real cost accounting.
- **PR #6 (this branch)**: site-neutral listing capture (user-activated
  `scripting` injection, generic Layer A/B/C extractor, Autotrader kept
  as a precision rule), operator review with photo selection and manual
  entry, user-supplied photo provenance, operator-requested ACC checks,
  photo-gated audit queueing, PASS / INCONCLUSIVE / FAIL display with an
  ACC-unavailable state, Verify-ACC end-to-end test with scripted
  adapters.
- Later: more dealership sites, reviewer QA workflow, evidence packets,
  packaging hardening.

## Fixture provenance & license

All images and listings under `fixtures/` are generated in-repo (`fixtures/generate.py`)
and watermarked SYNTHETIC. No real dealership data, no stock photos, no
tracking pixels. Licensed for public hackathon distribution — see
[CHANGELOG](CHANGELOG.md) for PR history.
