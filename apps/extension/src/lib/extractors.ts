/** Listing-page extractors.
 *
 * Site-specific rules (synthetic fixture, Autotrader) are precision
 * improvements. The generic extractor is the default fallback for
 * unrecognized websites: structured data first (Layer A), generic DOM
 * analysis second (Layer B), scored heuristic interpretation third
 * (Layer C). No new code deployment is needed for a new dealership site.
 */

import type { ExtractedListing } from './protocol';

export interface ExtractorRule {
  id: string;
  /** Host suffixes this rule handles, e.g. ["127.0.0.1:8901"]. */
  hosts: string[];
  matches(url: string): boolean;
  extract(doc: Document, url: string): ExtractedListing | null;
}

function text(doc: Document, selector: string): string {
  return doc.querySelector(selector)?.textContent?.trim() ?? '';
}

function attr(doc: Document, selector: string, name: string): string {
  return doc.querySelector(selector)?.getAttribute(name)?.trim() ?? '';
}

function absUrl(raw: string, base: string): string | null {
  try {
    const resolved = new URL(raw, base);
    if (resolved.protocol !== 'http:' && resolved.protocol !== 'https:') return null;
    return resolved.toString();
  } catch {
    return null;
  }
}

/** Synthetic fixture pages served from fixtures/synthetic-listings. */
export const syntheticExtractor: ExtractorRule = {
  id: 'synthetic-v1',
  hosts: ['127.0.0.1'],
  matches(url: string): boolean {
    try {
      const parsed = new URL(url);
      return parsed.hostname === '127.0.0.1' && parsed.port === '8901';
    } catch {
      return false;
    }
  },
  extract(doc: Document, url: string): ExtractedListing | null {
    const root = doc.querySelector('[data-vcc-listing]');
    if (!root) return null;
    const get = (name: string): string => root.getAttribute(`data-vcc-${name}`)?.trim() ?? '';
    const yearRaw = get('year');
    const year = /^\d{4}$/.test(yearRaw) ? Number(yearRaw) : null;
    const photos = Array.from(root.querySelectorAll('img[data-vcc-photo]'))
      .map((img) => img.getAttribute('src') ?? '')
      .map((src) => absUrl(src, url))
      .filter((href): href is string => href !== null)
      .slice(0, 40)
      .map((href) => ({ url: href }));
    const sourceSite = attr(doc, '[data-vcc-source-site]', 'data-vcc-source-site');
    return {
      sourceUrl: url,
      sourceSite: sourceSite || '127.0.0.1:8901',
      title: get('title') || text(doc, '[data-vcc-title]') || doc.title,
      year,
      make: get('make'),
      model: get('model'),
      trim: get('trim'),
      vin: get('vin') === '' ? null : get('vin'),
      description: text(doc, '[data-vcc-description]'),
      advertisedClaims: Array.from(doc.querySelectorAll('[data-vcc-claim]'))
        .map((el) => el.textContent?.trim() ?? '')
        .filter(Boolean)
        .slice(0, 200),
      photos,
      extractorId: 'synthetic-v1',
    };
  },
};

/**
 * Autotrader vehicle-detail extractor (ADR 002).
 *
 * JSON-LD `Car`/`Vehicle` first, OpenGraph + DOM fallbacks second. Search,
 * dealer-index, and sell pages are rejected: extraction requires a positive
 * VDP signal, so a search-results page can never be mistaken for a listing.
 * Filter selections (e.g. an ACC search filter) never appear here — an
 * operator-requested ACC check travels separately via `requested_checks`
 * and is never merged into `advertisedClaims`.
 */
export const autotraderExtractor: ExtractorRule = {
  id: 'autotrader-v1',
  hosts: ['www.autotrader.com', 'autotrader.com'],
  matches(url: string): boolean {
    try {
      const host = new URL(url).hostname.toLowerCase();
      return host === 'autotrader.com' || host.endsWith('.autotrader.com');
    } catch {
      return false;
    }
  },
  extract(doc: Document, url: string): ExtractedListing | null {
    if (isAutotraderSearchPage(doc, url)) return null;
    const jsonLd = readJsonLdNodes(doc);
    const vehicle = pickVehicleNode(jsonLd);
    const canonical = canonicalListingUrl(doc, url);
    // Positive VDP signal required: a JSON-LD vehicle with identifying
    // data, or a canonical vehicle URL plus gallery content.
    const hasVehicleData =
      vehicle !== null &&
      (vehicle.vin !== '' ||
        (vehicle.year !== null && vehicle.make !== '') ||
        vehicle.title !== '');
    const hasGallery = galleryPhotoUrls(doc, canonical, 1).length > 0;
    if (!hasVehicleData && !hasGallery) return null;
    if (!hasVehicleData && isLikelySearchUrl(url)) return null;

    const title =
      vehicle?.title ||
      metaContent(doc, 'meta[property="og:title"]') ||
      text(doc, 'h1') ||
      doc.title;
    const description =
      vehicle?.description ||
      metaContent(doc, 'meta[property="og:description"]') ||
      metaContent(doc, 'meta[name="description"]');
    const photos = collectAutotraderPhotos(doc, vehicle, canonical);
    const advertisedClaims = collectAutotraderClaims(doc, vehicle);
    const sourceSite = 'www.autotrader.com';
    return {
      sourceUrl: canonical,
      sourceSite,
      title: title.trim().slice(0, 500) || canonical,
      year: vehicle?.year ?? parseYearFromTitle(title),
      make: vehicle?.make ?? '',
      model: vehicle?.model ?? '',
      trim: vehicle?.trim ?? '',
      vin: vehicle?.vin || null,
      description: description.trim(),
      advertisedClaims,
      photos,
      extractorId: 'autotrader-v1',
    };
  },
};

/**
 * Generic vehicle-listing extractor (default fallback for unrecognized
 * websites). Layers:
 *
 * A. Structured data: Schema.org Car/Vehicle/Product JSON-LD, vehicle
 *    Offer blocks, OpenGraph tags — same readers as the site rules.
 * B. Generic DOM analysis: main heading, spec tables/dl rows, feature
 *    lists, gallery images (src, srcset best-variant, lazy attributes).
 * C. Heuristic interpretation with scoring: identity fields are combined,
 *    never invented. Confidence + per-field provenance travel in
 *    `extraction` so the operator review UI can show what was detected.
 *
 * Rejects index/search/ad pages: a page needs a vehicle signal (VIN,
 * year+make, or a titled gallery) to be treated as a listing, and pages
 * with search markers or several distinct vehicles are refused.
 */
export const genericExtractor: ExtractorRule = {
  id: 'generic-v1',
  hosts: [],
  matches(url: string): boolean {
    try {
      const protocol = new URL(url).protocol;
      return protocol === 'http:' || protocol === 'https:';
    } catch {
      return false;
    }
  },
  extract(doc: Document, url: string): ExtractedListing | null {
    const nodes = readJsonLdNodes(doc);
    if (isIndexPage(doc, url, nodes)) return null;
    const vehicle = pickVehicleNode(nodes);
    const specs = readSpecRows(doc);
    const title =
      vehicle?.title ||
      metaContent(doc, 'meta[property="og:title"]') ||
      text(doc, 'h1') ||
      doc.title;
    const titleYear = parseYearFromTitle(title);
    const year = vehicle?.year ?? parseYear(specs.year) ?? titleYear ?? null;
    const make = vehicle?.make || specs.make || '';
    const model = vehicle?.model || specs.model || '';
    const trim = vehicle?.trim || specs.trim || '';
    const vin = vehicle?.vin || specs.vin || null;
    const description =
      vehicle?.description ||
      metaContent(doc, 'meta[property="og:description"]') ||
      metaContent(doc, 'meta[name="description"]') ||
      text(doc, 'article p');
    const photos = collectGenericPhotos(doc, vehicle, url);
    const advertisedClaims = collectGenericClaims(doc, vehicle);

    // A bare title year counts as weak identity: a titled gallery with a
    // model year is plausibly a listing (low confidence, operator review),
    // while a car photo with no identity signal at all is refused.
    const hasIdentity =
      vin !== null || (year !== null && make !== '') || model !== '' || titleYear !== null;
    const hasEvidence = photos.length > 0 || advertisedClaims.length > 0;
    if (!hasIdentity && title.trim() === '') return null;
    if (!hasIdentity && !hasEvidence) return null;
    if (!hasIdentity && photos.length > 0) return null;

    const structured = vehicle !== null;
    const reasons: string[] = [];
    if (vin) reasons.push('VIN ' + (structured ? 'from structured data' : 'from spec rows'));
    else reasons.push('no VIN found');
    if (year !== null && make !== '') reasons.push('year and make identified');
    else reasons.push('year/make incomplete');
    if (model !== '') reasons.push('model identified');
    reasons.push(
      structured ? 'structured vehicle data present' : 'no structured vehicle data — DOM fallback',
    );
    reasons.push(
      `${photos.length} listing photo(s), ${advertisedClaims.length} equipment claim(s)`,
    );
    // high: VIN (or full structured identity) plus photos. medium: real
    // identity (year+make, or a model with evidence). low: title-year only
    // or missing evidence — operator review expected.
    const confidence: 'high' | 'medium' | 'low' =
      (vin !== null || (structured && year !== null && make !== '' && model !== '')) &&
      photos.length > 0
        ? 'high'
        : vin !== null || (year !== null && make !== '') || (model !== '' && hasEvidence)
          ? 'medium'
          : 'low';
    if (confidence === 'low') {
      reasons.push('low confidence — operator review recommended');
    }
    const fieldSources: Record<string, 'structured' | 'dom' | 'inferred'> = {};
    const mark = (
      field: string,
      value: string | null,
      fromStructured: boolean,
      fromDom: boolean,
    ): void => {
      if (value === null || value === '') return;
      fieldSources[field] = fromStructured ? 'structured' : fromDom ? 'dom' : 'inferred';
    };
    mark(
      'year',
      year === null ? null : String(year),
      (vehicle?.year ?? null) !== null,
      specs.year !== '',
    );
    mark('make', make, (vehicle?.make ?? '') !== '', specs.make !== '');
    mark('model', model, (vehicle?.model ?? '') !== '', specs.model !== '');
    mark('trim', trim, (vehicle?.trim ?? '') !== '', specs.trim !== '');
    mark('vin', vin, (vehicle?.vin ?? '') !== '', specs.vin !== '');
    mark('title', title.trim(), (vehicle?.title ?? '') !== '', text(doc, 'h1') !== '');
    mark('description', description.trim(), (vehicle?.description ?? '') !== '', true);
    if (advertisedClaims.length > 0) {
      fieldSources['claims'] = (vehicle?.features.length ?? 0) > 0 ? 'structured' : 'dom';
    }
    if (photos.length > 0) {
      fieldSources['photos'] = (vehicle?.images.length ?? 0) > 0 ? 'structured' : 'dom';
    }

    let hostname: string;
    try {
      hostname = new URL(url).hostname.toLowerCase();
    } catch {
      hostname = '';
    }
    return {
      sourceUrl: stripTrackingParams(url),
      sourceSite: hostname,
      title: title.trim().slice(0, 500) || url,
      year,
      make,
      model,
      trim,
      vin,
      description: description.trim(),
      advertisedClaims,
      photos,
      extractorId: 'generic-v1',
      extraction: { confidence, reasons, fieldSources },
    };
  },
};

/** Labeled spec rows: th/td and dt/dd pairs whose label names a vehicle
 * field. Generic across dealership templates; values are taken verbatim. */
function readSpecRows(doc: Document): {
  year: string;
  make: string;
  model: string;
  trim: string;
  vin: string;
} {
  const specs = { year: '', make: '', model: '', trim: '', vin: '' };
  const labelOf = (label: string): keyof typeof specs | null => {
    const key = label.toLowerCase().replace(/[^a-z]/g, '');
    if (['year', 'modelyear', 'yearmodel'].includes(key)) return 'year';
    if (['make', 'manufacturer', 'brand', 'oem'].includes(key)) return 'make';
    if (['model', 'vehiclemodel', 'modelname'].includes(key)) return 'model';
    if (['trim', 'trimlevel', 'vehicleconfiguration', 'package', 'edition'].includes(key))
      return 'trim';
    if (['vin', 'vehiclenumber', 'vehicleidentificationnumber', 'stock'].includes(key))
      return 'vin';
    return null;
  };
  const take = (label: string, value: string): void => {
    const field = labelOf(label);
    const cleaned = value.replace(/\s+/g, ' ').trim();
    if (field && cleaned !== '' && cleaned.length <= 120 && specs[field] === '') {
      specs[field] = cleaned;
    }
  };
  for (const row of Array.from(doc.querySelectorAll('tr'))) {
    const cells = Array.from(row.querySelectorAll('th, td'));
    if (cells.length >= 2) take(cells[0].textContent ?? '', cells[1].textContent ?? '');
  }
  for (const dl of Array.from(doc.querySelectorAll('dl'))) {
    const terms = Array.from(dl.querySelectorAll('dt'));
    for (const term of terms) {
      const def = term.nextElementSibling;
      if (def && def.tagName.toLowerCase() === 'dd') {
        take(term.textContent ?? '', def.textContent ?? '');
      }
    }
  }
  return specs;
}

/** Index/search/advertisement rejection for arbitrary sites: explicit
 * search markers, result-list structured data, or several distinct
 * vehicles on one page (dealer inventory, recommendations). */
function isIndexPage(doc: Document, url: string, nodes: Record<string, unknown>[]): boolean {
  let path: string;
  try {
    path = new URL(url).pathname.toLowerCase();
  } catch {
    return true;
  }
  const searchUrl =
    path.includes('/search') ||
    path.includes('searchresults') ||
    path.endsWith('/inventory') ||
    path.endsWith('/used-cars') ||
    path.endsWith('/new-cars');
  if (searchUrl) return true;
  if (
    doc.querySelector(
      '[data-cmp*="search" i], .search-results, #searchResults, [data-testid*="search-result" i], .srp-list',
    )
  ) {
    return true;
  }
  let listOnly = false;
  const vins = new Set<string>();
  const names = new Set<string>();
  for (const node of nodes) {
    const types = nodeTypes(node);
    if (types.some((t) => SEARCH_TYPES.has(t))) listOnly = true;
    if (!types.some((t) => VEHICLE_TYPES.has(t))) continue;
    const vin = nameOf(node['vehicleIdentificationNumber']).toUpperCase();
    if (vin !== '') vins.add(vin);
    const name = nameOf(node['name']).toLowerCase();
    if (name !== '') names.add(name);
  }
  if (listOnly && vins.size === 0) return true;
  // Several distinct vehicles on one page: an index, not a listing.
  if (vins.size > 1 || names.size > 3) return true;
  return false;
}

interface PhotoCandidate {
  url: string;
  alt: string;
  cls: string;
  width: number;
  height: number;
  rank: number;
}

/** Conservative non-listing filter: logos, pixels, ads, recommendations,
 * navigation chrome. Never drops a photo for being small alone. */
const PHOTO_EXCLUDE_PATTERN =
  /logo|sprite|pixel|spacer|transparent|placeholder|advert|banner|sponsor|recommended|related-vehicle|favicon|tracking|beacon|blank\.gif|1x1/i;

function candidateExcluded(candidate: PhotoCandidate, element: Element | null): boolean {
  if (PHOTO_EXCLUDE_PATTERN.test(candidate.url)) return true;
  if (PHOTO_EXCLUDE_PATTERN.test(`${candidate.alt} ${candidate.cls}`)) return true;
  if (candidate.width > 0 && candidate.width <= 2) return true;
  if (candidate.height > 0 && candidate.height <= 2) return true;
  let node: Element | null = element;
  for (let depth = 0; depth < 4 && node; depth += 1) {
    const tag = node.tagName.toLowerCase();
    if (tag === 'header' || tag === 'footer' || tag === 'nav') return true;
    const marker = `${node.getAttribute('class') ?? ''} ${node.getAttribute('id') ?? ''}`;
    if (/advert|sponsor|recommended|related|sidebar|popup|modal/i.test(marker)) return true;
    node = node.parentElement;
  }
  return false;
}

function galleryCandidates(doc: Document): { candidate: PhotoCandidate; element: Element }[] {
  const out: { candidate: PhotoCandidate; element: Element }[] = [];
  const push = (element: Element, raw: string | null | undefined, rank: number): void => {
    if (!raw) return;
    const img = element as unknown as {
      getAttribute?: (name: string) => string | null;
    };
    out.push({
      candidate: {
        url: raw.trim(),
        alt: img.getAttribute?.('alt') ?? '',
        cls: `${img.getAttribute?.('class') ?? ''} ${img.getAttribute?.('id') ?? ''}`,
        width: Number(img.getAttribute?.('width') ?? 0) || 0,
        height: Number(img.getAttribute?.('height') ?? 0) || 0,
        rank,
      },
      element,
    });
  };
  for (const img of Array.from(doc.querySelectorAll('img'))) {
    const best =
      pickBestSrc(img.getAttribute('srcset') ?? '') ??
      pickBestSrc(img.getAttribute('data-srcset') ?? '');
    if (best) push(img, best, 2);
    else push(img, img.getAttribute('src'), 1);
    push(img, img.getAttribute('data-src'), 1);
    push(img, img.getAttribute('data-lazy-src'), 1);
    push(img, img.getAttribute('data-lazy'), 1);
    push(img, img.getAttribute('data-original'), 1);
  }
  for (const source of Array.from(doc.querySelectorAll('picture source[srcset]'))) {
    const best = pickBestSrc(source.getAttribute('srcset') ?? '');
    if (best) {
      out.push({
        candidate: { url: best, alt: '', cls: '', width: 0, height: 0, rank: 2 },
        element: source,
      });
    }
  }
  return out;
}

/** Generic gallery collection: structured images first, then gallery
 * candidates with non-listing images excluded. Cross-origin CDN URLs are
 * kept — permission is handled at download time, never by dropping. */
function collectGenericPhotos(
  doc: Document,
  vehicle: JsonLdVehicle | null,
  base: string,
): { url: string }[] {
  const out: { url: string }[] = [];
  const seen = new Set<string>();
  const push = (raw: string): void => {
    if (out.length >= 40) return;
    const resolved = absUrl(raw.trim(), base);
    if (!resolved || seen.has(resolved)) return;
    seen.add(resolved);
    out.push({ url: resolved });
  };
  for (const raw of vehicle?.images ?? []) push(raw);
  const ogImage = metaContent(doc, 'meta[property="og:image"]');
  if (ogImage !== '') push(ogImage);
  // Prefer srcset best-variants over fallback srcs, then by document order.
  const ranked = galleryCandidates(doc)
    .map(({ candidate, element }) => ({
      candidate,
      element,
      resolved: absUrl(candidate.url, base),
    }))
    .filter(
      (
        entry,
      ): entry is {
        candidate: PhotoCandidate & { rank: number };
        element: Element;
        resolved: string;
      } => entry.resolved !== null,
    )
    .sort((a, b) => b.candidate.rank - a.candidate.rank);
  for (const { candidate, element, resolved } of ranked) {
    if (out.length >= 40) break;
    if (seen.has(resolved)) continue;
    if (candidateExcluded({ ...candidate }, element)) continue;
    seen.add(resolved);
    out.push({ url: resolved });
  }
  return out;
}

/** Generic equipment claims: structured features verbatim first, then
 * feature/equipment/spec/option list items. Never synthesized. */
function collectGenericClaims(doc: Document, vehicle: JsonLdVehicle | null): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const push = (raw: string): void => {
    const cleaned = raw.replace(/\s+/g, ' ').trim();
    if (cleaned === '' || cleaned.length > 200 || out.length >= 200) return;
    const key = cleaned.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    out.push(cleaned);
  };
  for (const feature of vehicle?.features ?? []) push(feature);
  const lists = doc.querySelectorAll(
    'ul[class*="feature" i] li, ol[class*="feature" i] li, ' +
      '[class*="equipment-list" i] li, [class*="amenities" i] li, ' +
      '[class*="options-list" i] li, [class*="spec-list" i] li, ' +
      '[data-testid*="feature" i] li, [data-cmp*="feature" i] li, section li',
  );
  for (const item of Array.from(lists)) {
    // Avoid nav/footer/ad lists: only items in main content.
    let node: Element | null = item;
    let bad = false;
    for (let depth = 0; depth < 5 && node; depth += 1) {
      const tag = node.tagName.toLowerCase();
      if (tag === 'header' || tag === 'footer' || tag === 'nav') {
        bad = true;
        break;
      }
      node = node.parentElement;
    }
    if (bad) continue;
    push(item.textContent ?? '');
  }
  return out;
}

/** Registry order is precedence: site precision rules first, the generic
 * fallback last. Unrecognized http(s) pages reach the generic extractor,
 * which still refuses anything without a vehicle signal. */
export const extractorRegistry: ExtractorRule[] = [
  syntheticExtractor,
  autotraderExtractor,
  genericExtractor,
];

export function extractorForUrl(url: string): ExtractorRule | null {
  return extractorRegistry.find((rule) => rule.matches(url)) ?? null;
}

interface JsonLdVehicle {
  title: string;
  year: number | null;
  make: string;
  model: string;
  trim: string;
  vin: string;
  description: string;
  images: string[];
  features: string[];
}

function metaContent(doc: Document, selector: string): string {
  return doc.querySelector(selector)?.getAttribute('content')?.trim() ?? '';
}

function nameOf(value: unknown): string {
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'object' && value !== null) {
    const name = (value as Record<string, unknown>).name;
    if (typeof name === 'string') return name.trim();
  }
  return '';
}

function parseYear(value: unknown): number | null {
  const text = String(value ?? '').trim();
  const match = text.match(/\b(19\d{2}|20\d{2}|21\d{2})\b/);
  if (!match) return null;
  const year = Number(match[1]);
  return year >= 1886 && year <= 2100 ? year : null;
}

function parseYearFromTitle(title: string): number | null {
  return parseYear(title);
}

function asStringArray(value: unknown): string[] {
  const items = Array.isArray(value) ? value : [value];
  const out: string[] = [];
  for (const item of items) {
    if (typeof item === 'string' && item.trim() !== '') out.push(item.trim());
    else if (typeof item === 'object' && item !== null) {
      const record = item as Record<string, unknown>;
      const url =
        typeof record.url === 'string'
          ? record.url
          : typeof record.contentUrl === 'string'
            ? record.contentUrl
            : nameOf(item);
      if (url.trim() !== '') out.push(url.trim());
    }
  }
  return out;
}

/** Flatten JSON-LD blocks (arrays, @graph) into candidate object nodes. */
function readJsonLdNodes(doc: Document): Record<string, unknown>[] {
  const nodes: Record<string, unknown>[] = [];
  const push = (value: unknown): void => {
    if (Array.isArray(value)) {
      for (const item of value) push(item);
      return;
    }
    if (typeof value !== 'object' || value === null) return;
    const record = value as Record<string, unknown>;
    nodes.push(record);
    if (record['@graph'] !== undefined) push(record['@graph']);
  };
  for (const script of Array.from(doc.querySelectorAll('script[type="application/ld+json"]'))) {
    try {
      push(JSON.parse(script.textContent ?? ''));
    } catch {
      // Malformed blocks are page bugs, not listings: skip them.
    }
  }
  return nodes;
}

const SEARCH_TYPES = new Set(['searchresultspage', 'itemlist', 'collectionpage', 'searchpage']);
const VEHICLE_TYPES = new Set(['car', 'vehicle', 'motorvehicle', 'product']);

function nodeTypes(node: Record<string, unknown>): string[] {
  const raw = node['@type'];
  const list = Array.isArray(raw) ? raw : [raw];
  return list.filter((t): t is string => typeof t === 'string').map((t) => t.toLowerCase());
}

/** Best JSON-LD vehicle node, or null when the page carries none. */
function pickVehicleNode(nodes: Record<string, unknown>[]): JsonLdVehicle | null {
  let best: { score: number; node: Record<string, unknown> } | null = null;
  for (const node of nodes) {
    const types = nodeTypes(node);
    if (types.some((t) => SEARCH_TYPES.has(t))) continue;
    if (!types.some((t) => VEHICLE_TYPES.has(t))) continue;
    const vin = (
      nameOf(node['vehicleIdentificationNumber']) ||
      nameOf(node['vin']) ||
      nameOf(node['serialNumber'])
    )
      .toUpperCase()
      .replace(/[^A-Z0-9]/g, '');
    const year =
      parseYear(node['modelDate']) ??
      parseYear(node['vehicleModelDate']) ??
      parseYear(node['productionDate']);
    const make = nameOf(node['manufacturer']) || nameOf(node['make']) || nameOf(node['brand']);
    const title = nameOf(node['name']);
    let score = 0;
    if (vin !== '') score += 3;
    if (year !== null && make !== '') score += 2;
    if (title !== '') score += 1;
    if (node['image'] !== undefined) score += 1;
    if (best === null || score > best.score) best = { score, node };
  }
  if (best === null || best.score < 2) return null;
  const node = best.node;
  return {
    title: nameOf(node['name']),
    year:
      parseYear(node['modelDate']) ??
      parseYear(node['vehicleModelDate']) ??
      parseYear(node['productionDate']),
    make: nameOf(node['manufacturer']) || nameOf(node['make']) || nameOf(node['brand']),
    model: nameOf(node['model']) || nameOf(node['vehicleModel']),
    trim: nameOf(node['vehicleConfiguration']) || nameOf(node['trim']),
    vin: (
      nameOf(node['vehicleIdentificationNumber']) ||
      nameOf(node['vin']) ||
      nameOf(node['serialNumber'])
    )
      .toUpperCase()
      .replace(/[^A-Z0-9]/g, ''),
    description: nameOf(node['description']),
    images: asStringArray(node['image']),
    features: [
      ...asStringArray(node['vehicleFeature']),
      ...asStringArray(node['amenityFeature']),
      ...asStringArray(node['features']),
    ],
  };
}

const TRACKING_PARAMS = new Set([
  'utm_source',
  'utm_medium',
  'utm_campaign',
  'utm_term',
  'utm_content',
  'gclid',
  'fbclid',
  'resultsetid',
  'searchuuid',
  'srpvariation',
  'listingindex',
  'searchzip',
  'searchradius',
  'searchdistance',
]);

/** Canonical identity: canonical/og:url when same-origin, else the live URL
 * with session/tracking parameters stripped. */
function canonicalListingUrl(doc: Document, url: string): string {
  const candidates = [
    doc.querySelector('link[rel="canonical"]')?.getAttribute('href') ?? '',
    metaContent(doc, 'meta[property="og:url"]'),
  ];
  for (const raw of candidates) {
    const resolved = absUrl(raw, url);
    if (!resolved) continue;
    try {
      if (new URL(resolved).hostname.toLowerCase().endsWith('autotrader.com')) {
        return stripTrackingParams(resolved);
      }
    } catch {
      continue;
    }
  }
  return stripTrackingParams(url);
}

function stripTrackingParams(raw: string): string {
  try {
    const parsed = new URL(raw);
    for (const key of Array.from(parsed.searchParams.keys())) {
      if (TRACKING_PARAMS.has(key.toLowerCase())) parsed.searchParams.delete(key);
    }
    return parsed.toString();
  } catch {
    return raw;
  }
}

function isLikelySearchUrl(url: string): boolean {
  let path: string;
  try {
    path = new URL(url).pathname.toLowerCase();
  } catch {
    return false;
  }
  if (path.includes('/search')) return true;
  return (
    path === '/car-dealers' ||
    path.startsWith('/car-dealers/') ||
    path === '/sell-my-car' ||
    path.startsWith('/sell-my-car') ||
    path === '/cars-for-sale/all-cars' ||
    path === '/used-cars' ||
    path === '/new-cars'
  );
}

function isAutotraderSearchPage(doc: Document, url: string): boolean {
  if (isLikelySearchUrl(url)) return true;
  const marker = doc.querySelector(
    '[data-cmp*="search" i], .search-results, #searchResults, [data-testid*="search-result" i]',
  );
  if (!marker) return false;
  // A results grid without a VIN-bearing vehicle node is not a VDP.
  return !readJsonLdNodes(doc).some((node) => {
    if (!nodeTypes(node).some((t) => VEHICLE_TYPES.has(t))) return false;
    const vin = nameOf(node['vehicleIdentificationNumber']) || nameOf(node['vin']);
    return vin.trim() !== '';
  });
}

/** Largest-width entry of a srcset, or null when unparsable. */
function pickBestSrc(srcset: string): string | null {
  let best: { url: string; rank: number } | null = null;
  for (const entry of srcset.split(',')) {
    const parts = entry.trim().split(/\s+/);
    if (parts.length === 0 || parts[0] === '') continue;
    const descriptor = parts[1] ?? '';
    let rank = 0;
    if (descriptor.endsWith('w')) rank = Number(descriptor.slice(0, -1)) || 0;
    else if (descriptor.endsWith('x')) rank = (Number(descriptor.slice(0, -1)) || 0) * 1000;
    if (best === null || rank >= best.rank) best = { url: parts[0], rank };
  }
  return best?.url ?? null;
}

/** Gallery URLs (lazy-loaded variants included), resolved + capped. */
function galleryPhotoUrls(doc: Document, base: string, limit: number): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const push = (raw: string | null | undefined): void => {
    if (out.length >= limit || !raw) return;
    const resolved = absUrl(raw.trim(), base);
    if (!resolved || seen.has(resolved)) return;
    seen.add(resolved);
    out.push(resolved);
  };
  for (const img of Array.from(doc.querySelectorAll('img'))) {
    // Prefer the largest srcset variant: `src` is usually the fallback.
    const best =
      pickBestSrc(img.getAttribute('srcset') ?? '') ??
      pickBestSrc(img.getAttribute('data-srcset') ?? '');
    if (best) push(best);
    else push(img.getAttribute('src'));
    push(img.getAttribute('data-src'));
    push(img.getAttribute('data-lazy-src'));
    push(img.getAttribute('data-lazy'));
    push(img.getAttribute('data-original'));
  }
  for (const source of Array.from(doc.querySelectorAll('picture source[srcset]'))) {
    push(pickBestSrc(source.getAttribute('srcset') ?? ''));
  }
  return out;
}

function collectAutotraderPhotos(
  doc: Document,
  vehicle: JsonLdVehicle | null,
  base: string,
): { url: string }[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const push = (raw: string): void => {
    if (out.length >= 40) return;
    const resolved = absUrl(raw.trim(), base);
    if (!resolved || seen.has(resolved)) return;
    seen.add(resolved);
    out.push(resolved);
  };
  for (const raw of vehicle?.images ?? []) push(raw);
  const ogImage = metaContent(doc, 'meta[property="og:image"]');
  if (ogImage !== '') push(ogImage);
  for (const raw of galleryPhotoUrls(doc, base, 40)) push(raw);
  return out.map((url) => ({ url }));
}

function collectAutotraderClaims(doc: Document, vehicle: JsonLdVehicle | null): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const push = (raw: string): void => {
    const cleaned = raw.replace(/\s+/g, ' ').trim();
    if (cleaned === '' || cleaned.length > 200 || out.length >= 200) return;
    const key = cleaned.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    out.push(cleaned);
  };
  // Seller feature wording first, verbatim: never synthesized, never mapped.
  for (const feature of vehicle?.features ?? []) push(feature);
  // DOM feature/spec lists as a fallback.
  const lists = doc.querySelectorAll(
    '[data-cmp*="feature" i] li, [class*="feature-list" i] li, #features li, [data-testid*="feature" i] li',
  );
  for (const item of Array.from(lists)) push(item.textContent ?? '');
  return out;
}

export function allowedContentHosts(): Set<string> {
  const hosts = new Set<string>();
  for (const rule of extractorRegistry) {
    for (const host of rule.hosts) hosts.add(host.toLowerCase());
  }
  return hosts;
}
