# ADR 002 — First real listing site: Autotrader

Date: 2026-11-04. Status: accepted (PR #6, hackathon milestone A).

## Decision

Support `autotrader.com` (www host) first. Defer CarGurus until the
Autotrader path is verified end to end.

## Why Autotrader first

- **Stable listing identity.** Autotrader vehicle-detail (VDP) URLs carry a
  stable vehicle/listing identifier suitable for `source_url`
  deduplication. CarGurus detail URLs in the wild carry session-scoped
  parameters (`resultSetId`, `searchUuid`, `srpVariation`, `listingIndex`)
  that change per search, so the same car can present many URLs.
- **Machine-readable vehicle block.** Autotrader VDPs commonly embed
  JSON-LD `Car`/`Vehicle` + `Offer` (year, make, model, configuration,
  VIN, image array) plus OpenGraph tags. CarGurus search reactivity loads
  results from post-load JSON endpoints whose formats are undocumented and
  change, which is a weaker foundation for a heuristic extractor.
- **Permission scope.** One primary site host plus its image CDN family
  keeps `host_permissions` narrow. No `<all_urls>`.

## What the extractor does not do

- It never assumes a search-filter selection (e.g. an "Adaptive Cruise
  Control" filter tick) appears on the detail page. A user-requested ACC
  check is stored as `requested_checks`, never merged into
  `advertised_claims`.
- It never bypasses login, anti-bot (e.g. PerimeterX/HUMAN) challenges, or
  access controls. The extension runs inside the operator's own browsing
  session, so challenges are solved by the operator's normal browsing;
  there is no programmatic bypass. Pages that do not render for the
  operator yield no listing (`null`), never a fabricated one.
- Generic cruise controls (`cruise control` without an adaptive/dynamic/
  radar qualifier) are preserved verbatim as seller text but are never
  promoted to ACC. The `normalize_feature` taxonomy keeps
  `cruise_control` and `adaptive_cruise_control` distinct.

## Consequences

- New `autotrader-v1` extractor rule, JSON-LD first with DOM fallbacks.
- Content-script matches + `host_permissions` gain the www host only;
  image CDN origins stay optional and failures surface as `permission`
  failures in the mother app, never silent drops.
- Synthetic fixture extractor and its tests are untouched.
