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
  /** Scoring + field-level provenance (generic extractor; optional). */
  extraction?: ExtractionDetail;
}

/** Where an extracted field came from. `operator` is set by the popup
 * review UI, never by page extractors. */
export type FieldSource = 'structured' | 'dom' | 'inferred' | 'operator';

export interface ExtractionDetail {
  /** Overall quality: high (VIN or full structured identity + photos),
   * medium (partial identity + evidence), low (title/photos only). */
  confidence: 'high' | 'medium' | 'low';
  /** Human-readable account of which signals fired or were missing. */
  reasons: string[];
  /** Per-field provenance for year/make/model/trim/vin/title/
   * description/claims/photos. Missing fields are simply absent. */
  fieldSources: Partial<Record<string, FieldSource>>;
}

/**
 * Operator-requested verification checks. Selected in the popup (e.g.
 * "Verify ACC"), never inferred from the page: a search-filter selection
 * must not become a seller claim. Single-value allowlist for the
 * real-listing milestone.
 */
export const REQUESTED_CHECKS = ['adaptive_cruise_control'] as const;
export type RequestedCheck = (typeof REQUESTED_CHECKS)[number];

export function isRequestedCheck(value: unknown): value is RequestedCheck {
  return typeof value === 'string' && (REQUESTED_CHECKS as readonly string[]).includes(value);
}

/**
 * Operator corrections applied on top of an extracted listing. Only
 * whitelisted scalar fields; claims and photos are selected, never typed.
 */
export interface ListingOverrides {
  title?: string;
  year?: number | null;
  make?: string;
  model?: string;
  trim?: string;
  description?: string;
}

/** Manual vehicle entry for pages where no vehicle was detected. */
export interface ManualEntry {
  title: string;
  year: number | null;
  make: string;
  model: string;
  trim: string;
  description: string;
}

/** Operator-selected local file, sent as a data URL (images only). */
export interface UserSuppliedFile {
  name: string;
  dataUrl: string;
}

export const MAX_USER_FILES = 10;
export const MAX_USER_FILE_CHARS = 20_000_000;
export const MAX_PHOTO_URLS = 40;

function isOptionalText(value: unknown, maxLength: number): boolean {
  return value === undefined || (typeof value === 'string' && value.length <= maxLength);
}

export function isListingOverrides(value: unknown): value is ListingOverrides {
  if (typeof value !== 'object' || value === null) return false;
  const o = value as Record<string, unknown>;
  const allowed = ['title', 'year', 'make', 'model', 'trim', 'description'];
  if (Object.keys(o).some((k) => !allowed.includes(k))) return false;
  return (
    isOptionalText(o.title, 500) &&
    isOptionalText(o.make, 100) &&
    isOptionalText(o.model, 100) &&
    isOptionalText(o.trim, 100) &&
    isOptionalText(o.description, 20000) &&
    (o.year === undefined ||
      o.year === null ||
      (typeof o.year === 'number' && Number.isInteger(o.year)))
  );
}

export function isManualEntry(value: unknown): value is ManualEntry {
  if (typeof value !== 'object' || value === null) return false;
  const o = value as Record<string, unknown>;
  return (
    typeof o.title === 'string' &&
    o.title.length > 0 &&
    o.title.length <= 500 &&
    (o.year === null || (typeof o.year === 'number' && Number.isInteger(o.year))) &&
    typeof o.make === 'string' &&
    o.make.length <= 100 &&
    typeof o.model === 'string' &&
    o.model.length <= 100 &&
    typeof o.trim === 'string' &&
    o.trim.length <= 100 &&
    typeof o.description === 'string' &&
    o.description.length <= 20000
  );
}

export function isUserSuppliedFile(value: unknown): value is UserSuppliedFile {
  if (typeof value !== 'object' || value === null) return false;
  const file = value as Record<string, unknown>;
  return (
    typeof file.name === 'string' &&
    file.name.length > 0 &&
    file.name.length <= 255 &&
    typeof file.dataUrl === 'string' &&
    file.dataUrl.startsWith('data:image/') &&
    file.dataUrl.length <= MAX_USER_FILE_CHARS
  );
}

export type BackgroundRequest =
  | { type: 'vcc:extract-request' }
  | { type: 'vcc:get-state' }
  | { type: 'vcc:preview-extract'; tabId: number }
  | { type: 'vcc:check-hosts'; origins: string[] }
  | {
      type: 'vcc:start-import';
      tabId: number;
      requestedCheck?: RequestedCheck | null;
      overrides?: ListingOverrides;
      photoUrls?: string[];
      manualEntry?: ManualEntry | null;
      userFiles?: UserSuppliedFile[];
    }
  | { type: 'vcc:retry-failed' }
  | { type: 'vcc:abandon-import' }
  | { type: 'vcc:cancel-import' };

export type ContentResponse =
  | { type: 'vcc:extract-result'; listing: ExtractedListing | null }
  | { type: 'vcc:error'; message: string };

export type PreviewResponse =
  | { type: 'vcc:preview-result'; listing: ExtractedListing | null }
  | { type: 'vcc:error'; message: string };

export type HostAccessResponse =
  | { type: 'vcc:host-access'; access: Record<string, boolean> }
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
    case 'vcc:preview-extract':
      return typeof value.tabId === 'number' && Number.isInteger(value.tabId);
    case 'vcc:check-hosts': {
      const origins = (value as { origins?: unknown }).origins;
      return (
        Array.isArray(origins) &&
        origins.length <= 20 &&
        origins.every((o) => typeof o === 'string' && o.length > 0 && o.length <= 200)
      );
    }
    case 'vcc:start-import': {
      if (typeof value.tabId !== 'number' || !Number.isInteger(value.tabId)) {
        return false;
      }
      const typed = value as {
        requestedCheck?: unknown;
        overrides?: unknown;
        photoUrls?: unknown;
        manualEntry?: unknown;
        userFiles?: unknown;
      };
      const check = typed.requestedCheck;
      if (!(check === undefined || check === null || isRequestedCheck(check))) {
        return false;
      }
      if (typed.overrides !== undefined && !isListingOverrides(typed.overrides)) {
        return false;
      }
      if (
        typed.photoUrls !== undefined &&
        !(
          Array.isArray(typed.photoUrls) &&
          typed.photoUrls.length <= MAX_PHOTO_URLS &&
          typed.photoUrls.every((u) => typeof u === 'string')
        )
      ) {
        return false;
      }
      if (
        typed.manualEntry !== undefined &&
        typed.manualEntry !== null &&
        !isManualEntry(typed.manualEntry)
      ) {
        return false;
      }
      if (
        typed.userFiles !== undefined &&
        !(
          Array.isArray(typed.userFiles) &&
          typed.userFiles.length <= MAX_USER_FILES &&
          typed.userFiles.every(isUserSuppliedFile)
        )
      ) {
        return false;
      }
      return true;
    }
    default:
      return false;
  }
}

/** Validate a listing payload before the popup renders preview fields or
 * the background starts an import from it. */
export function isExtractedListing(value: unknown): value is ExtractedListing {
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
