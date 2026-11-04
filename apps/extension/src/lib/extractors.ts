/** Listing-page extractors. One rule module per supported site family; the
 * synthetic fixture extractor (data-vcc-* attributes) ships first and doubles
 * as the E2E target.
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

export const extractorRegistry: ExtractorRule[] = [syntheticExtractor, autotraderExtractor];

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
