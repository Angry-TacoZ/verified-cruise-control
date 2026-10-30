/** Typed runtime-message protocol between extension contexts.
 *
 * Content script → background carries JSON-serializable listing metadata and
 * image URLs only. Tokens, cookies, and image bytes never cross this
 * boundary; the background worker holds the pairing token and fetches bytes.
 */

export interface ExtractedPhoto {
  url: string;
}

export interface ExtractedListing {
  sourceUrl: string;
  sourceSite: string;
  title: string;
  year: number | null;
  make: string;
  model: string;
  trim: string;
  vin: string | null;
  description: string;
  advertisedClaims: string[];
  photos: ExtractedPhoto[];
  extractorId: string;
}

export type BackgroundRequest =
  | { type: 'vcc:extract-request' }
  | { type: 'vcc:get-state' }
  | { type: 'vcc:start-import'; tabId: number }
  | { type: 'vcc:retry-failed' }
  | { type: 'vcc:abandon-import' }
  | { type: 'vcc:cancel-import' };

export type ContentResponse =
  | { type: 'vcc:extract-result'; listing: ExtractedListing | null }
  | { type: 'vcc:error'; message: string };

export interface SenderInfo {
  tabId?: number;
  tabUrl?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

export function isBackgroundRequest(value: unknown): value is BackgroundRequest {
  if (!isRecord(value) || typeof value.type !== 'string') return false;
  switch (value.type) {
    case 'vcc:extract-request':
    case 'vcc:get-state':
    case 'vcc:retry-failed':
    case 'vcc:abandon-import':
    case 'vcc:cancel-import':
      return true;
    case 'vcc:start-import':
      return typeof value.tabId === 'number' && Number.isInteger(value.tabId);
    default:
      return false;
  }
}

function isExtractedListing(value: unknown): value is ExtractedListing {
  if (!isRecord(value)) return false;
  return (
    typeof value.sourceUrl === 'string' &&
    typeof value.sourceSite === 'string' &&
    typeof value.title === 'string' &&
    (value.year === null || typeof value.year === 'number') &&
    typeof value.make === 'string' &&
    typeof value.model === 'string' &&
    typeof value.trim === 'string' &&
    (value.vin === null || typeof value.vin === 'string') &&
    typeof value.description === 'string' &&
    Array.isArray(value.advertisedClaims) &&
    value.advertisedClaims.every((c) => typeof c === 'string') &&
    Array.isArray(value.photos) &&
    value.photos.every((p) => isRecord(p) && typeof p.url === 'string') &&
    typeof value.extractorId === 'string'
  );
}

export function isContentResponse(value: unknown): value is ContentResponse {
  if (!isRecord(value)) return false;
  if (value.type === 'vcc:extract-result') {
    return value.listing === null || isExtractedListing(value.listing);
  }
  if (value.type === 'vcc:error') return typeof value.message === 'string';
  return false;
}

/** Accept messages from extension pages and from tabs on hosts we have
 * extraction rules for. Extension pages normally carry no tab sender, but do
 * when loaded in a tab (tests, debugging) — same trust either way, since
 * only this extension's contexts can reach runtime messaging (no
 * externally_connectable declared).
 */
export function isAllowedSender(sender: SenderInfo, allowedTabHosts: Set<string>): boolean {
  if (sender.tabId === undefined) return true; // popup/options page
  if (!sender.tabUrl) return false;
  let parsed: URL;
  try {
    parsed = new URL(sender.tabUrl);
  } catch {
    return false;
  }
  if (parsed.protocol === 'chrome-extension:') return true;
  return allowedTabHosts.has(parsed.hostname.toLowerCase());
}

/** Structural guard: message payloads must never contain credential fields. */
const FORBIDDEN_KEYS = ['token', 'pairingToken', 'extensionToken', 'apiKey', 'password', 'cookie'];

export function containsForbiddenKeys(value: unknown): boolean {
  if (Array.isArray(value)) return value.some(containsForbiddenKeys);
  if (isRecord(value)) {
    return Object.entries(value).some(([key, entry]) => {
      const lowered = key.toLowerCase();
      if (FORBIDDEN_KEYS.some((forbidden) => lowered.includes(forbidden))) return true;
      return containsForbiddenKeys(entry);
    });
  }
  return false;
}
