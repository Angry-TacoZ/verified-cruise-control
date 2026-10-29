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

export const extractorRegistry: ExtractorRule[] = [syntheticExtractor];

export function extractorForUrl(url: string): ExtractorRule | null {
  return extractorRegistry.find((rule) => rule.matches(url)) ?? null;
}

export function allowedContentHosts(): Set<string> {
  const hosts = new Set<string>();
  for (const rule of extractorRegistry) {
    for (const host of rule.hosts) hosts.add(host.toLowerCase());
  }
  return hosts;
}
