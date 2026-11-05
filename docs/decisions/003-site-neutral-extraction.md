# ADR 003 — Site-neutral vehicle listing verification

Date: 2026-11-04. Status: accepted (PR #6 pivot, hackathon P0).

Supersedes the deployment model of ADR 002 (Autotrader-first), not its
extraction rules: `autotrader-v1` survives as a precision rule. What
changes is that no new code deployment is needed for the next site.

## Decision

The extension operates above the active website rather than depending on
preprogrammed support for individual marketplaces:

- Content scripts never auto-run on listing sites. The only registered
  match is the synthetic fixture server. Real pages are inspected by
  temporary, user-activated injection of the same bundled extractor via
  Manifest V3 `scripting` + `activeTab` (background `extractFromTab`:
  message a registered listener first, inject on demand otherwise).
- The generic extractor (`generic-v1`) is the default fallback for
  unrecognized sites. Layers: A — structured data (JSON-LD
  `Car`/`Vehicle`/`Product`, vehicle `Offer`, OpenGraph); B — generic DOM
  analysis (headings, spec rows, feature lists, galleries, srcset/lazy
  attributes); C — scored heuristic interpretation with per-field
  provenance and a confidence level. Missing fields stay unknown; index,
  search, and ad pages are rejected.
- The popup is a review step, not a blind trigger: detection confidence,
  seller claims (read-only), editable vehicle fields (corrections marked
  `operator`), a photo checklist, per-origin host grants via optional
  host permissions, manual entry for undetected pages, and
  operator-attached local files stored with `user_supplied` provenance.
- The backend stays authoritative: Muse/SAM inference, reconciliation,
  spend caps, operator-session audit queueing, and the five-state
  taxonomy are unchanged. A completed audit with no ACC finding reports
  **ACC finding unavailable**, never an evidence-based result.

## Why this instead of per-site extractors

Every new marketplace, dealer template, or markup change would otherwise
need a code change, a permission change, and a release. A generic layer
with operator review degrades gracefully (correct, select, attach) where
a site rule would silently fail, and permission boundaries stay intact:
temporary tab access, runtime per-origin grants, no `<all_urls>`, no
remotely fetched code, no credential reads, no anti-bot bypass.

## What this does not do

- It does not claim every vehicle website works. Bot-walled, login-walled,
  or gallery-blocked sites fall back to operator photo selection and
  manual entry, and blocked images are reported per photo.
- It does not invent evidence: overrides cover whitelisted scalars only;
  claims are never typed; photo URLs are selected, never entered; manual
  entries carry no seller claims.
- It does not weaken the extension-token/operator-session separation or
  the photo gate before paid inference.
