# Manual two-site verification checklist (PR #6)

Headless automation is bot-walled on real marketplaces, so this
acceptance test runs in James's own headed Chrome session. No paid
inference until James explicitly authorizes it: every step below is $0
unless the audit step is separately authorized.

## Setup (once)

1. Backend, mother app, and fixture server running
   (`./scripts/dev.ps1` from the repo root).
2. Extension built from this branch and loaded unpacked
   (`apps/extension/.output/chrome-mv3`), paired with the token from
   the mother app `#/pairing` page.
3. `VCC_INFERENCE_ENABLED` unset or `0` (fake adapters): imports and
   audit queueing work; provider calls are refused before any spend.

## Per site (do two independently structured sites, e.g. Autotrader + a
## dealership site; record the exact URLs visited, with no VINs/plates in
## notes)

1. Open an individual vehicle listing (not a search-results page).
2. Click the extension icon → **Review listing**.
3. Confirm the popup shows:
   - the detected year/make/model/trim/title and the extractor id plus
     confidence (`generic-v1` expected on unrecognized sites,
     `autotrader-v1` on Autotrader VDPs);
   - seller claims listed read-only (an ACC filter tick from search must
     NOT appear as a seller claim);
   - detected photos pre-selected.
4. If detection is wrong, correct a field; if photos are missing or
   uncertain, deselect/select photos. If the page grants no access,
   grant the image host when asked, or attach local files (they must
   appear as user-supplied, never page evidence).
5. Click **Verify Adaptive Cruise Control**. The import must complete or
   report per-photo failures (permission/validation/download) in the
   mother app — never silent zero-photo success.
6. Open the listing in the mother app: vehicle identity, `requested_checks`
   containing `adaptive_cruise_control`, original photos with hashes,
   and correct provenance (`extension` vs `user_supplied`).
7. Click **Run ACC Audit** (operator session). With fake adapters the
   audit settles without provider calls; with authorized live inference
   it must show the finding, observations, crops, model usage, and cost.
8. Dashboard badge: PASS, INCONCLUSIVE, or FAIL for a real finding;
   **ACC finding unavailable** if the completed audit produced none;
   operational states (queued/running/failed) shown as such, never as
   evidence results.

## Record for the PR report

- Site URLs (listing pages only, no personal data), extractor id and
  confidence per site, photos detected vs imported per site, manual
  corrections made, permission grants/denials, audit status and
  dashboard outcome, and — only if authorized — model ids, call counts,
  and reported spend.

Untested sites and unverified steps stay explicitly unverified in the
report; do not mark this acceptance test green on fixture evidence
alone.
