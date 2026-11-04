import { describe, expect, it } from 'vitest';
import {
  containsForbiddenKeys,
  isAllowedSender,
  isBackgroundRequest,
  isContentResponse,
  isExtractedListing,
  isListingOverrides,
  isManualEntry,
  isUserSuppliedFile,
  type ExtractedListing,
} from './protocol';

const listing: ExtractedListing = {
  sourceUrl: 'http://127.0.0.1:8901/l/1',
  sourceSite: 'example.test',
  title: 't',
  year: 1987,
  make: 'm',
  model: 'mo',
  trim: '',
  vin: null,
  description: 'd',
  advertisedClaims: ['cruise control'],
  photos: [{ url: 'http://127.0.0.1:8901/p.jpg' }],
  extractorId: 'synthetic-v1',
};

describe('protocol validation', () => {
  it('accepts well-formed background requests', () => {
    expect(isBackgroundRequest({ type: 'vcc:extract-request' })).toBe(true);
    expect(isBackgroundRequest({ type: 'vcc:get-state' })).toBe(true);
    expect(isBackgroundRequest({ type: 'vcc:start-import', tabId: 7 })).toBe(true);
    expect(isBackgroundRequest({ type: 'vcc:retry-failed' })).toBe(true);
    expect(isBackgroundRequest({ type: 'vcc:abandon-import' })).toBe(true);
    expect(isBackgroundRequest({ type: 'vcc:cancel-import' })).toBe(true);
  });

  it('rejects malformed background requests', () => {
    expect(isBackgroundRequest(null)).toBe(false);
    expect(isBackgroundRequest({ type: 'vcc:start-import' })).toBe(false);
    expect(isBackgroundRequest({ type: 'vcc:start-import', tabId: 'x' })).toBe(false);
    expect(isBackgroundRequest({ type: 'vcc:drop-database' })).toBe(false);
  });

  it('allow-lists the operator-requested ACC check on start-import', () => {
    expect(
      isBackgroundRequest({
        type: 'vcc:start-import',
        tabId: 7,
        requestedCheck: 'adaptive_cruise_control',
      }),
    ).toBe(true);
    expect(isBackgroundRequest({ type: 'vcc:start-import', tabId: 7 })).toBe(true);
    expect(isBackgroundRequest({ type: 'vcc:start-import', tabId: 7, requestedCheck: null })).toBe(
      true,
    );
    // Anything else is rejected: the check is operator-selected, never
    // inferred, so arbitrary values must not pass validation.
    expect(
      isBackgroundRequest({ type: 'vcc:start-import', tabId: 7, requestedCheck: 'self_driving' }),
    ).toBe(false);
    expect(isBackgroundRequest({ type: 'vcc:start-import', tabId: 7, requestedCheck: 42 })).toBe(
      false,
    );
  });

  it('accepts well-formed content responses', () => {
    expect(isContentResponse({ type: 'vcc:extract-result', listing })).toBe(true);
    expect(isContentResponse({ type: 'vcc:extract-result', listing: null })).toBe(true);
    expect(isContentResponse({ type: 'vcc:error', message: 'nope' })).toBe(true);
  });

  it('rejects malformed content responses', () => {
    expect(isContentResponse({ type: 'vcc:extract-result' })).toBe(false);
    expect(isContentResponse({ type: 'vcc:extract-result', listing: { title: 1 } })).toBe(false);
    expect(isContentResponse({ type: 'vcc:error' })).toBe(false);
    expect(isContentResponse('vcc:extract-result')).toBe(false);
  });

  it('validates senders by tab host', () => {
    const allowed = new Set(['127.0.0.1']);
    expect(isAllowedSender({}, allowed)).toBe(true); // popup
    expect(isAllowedSender({ tabId: 1, tabUrl: 'http://127.0.0.1:8901/l/1' }, allowed)).toBe(true);
    expect(isAllowedSender({ tabId: 1, tabUrl: 'https://evil.test/' }, allowed)).toBe(false);
    expect(isAllowedSender({ tabId: 1 }, allowed)).toBe(false);
    expect(
      isAllowedSender({ tabId: 2, tabUrl: 'chrome-extension://abc/popup.html' }, allowed),
    ).toBe(true); // extension page in a tab
  });

  it('validates preview and host-access requests', () => {
    expect(isBackgroundRequest({ type: 'vcc:preview-extract', tabId: 9 })).toBe(true);
    expect(isBackgroundRequest({ type: 'vcc:preview-extract' })).toBe(false);
    expect(isBackgroundRequest({ type: 'vcc:preview-extract', tabId: 1.5 })).toBe(false);
    expect(isBackgroundRequest({ type: 'vcc:check-hosts', origins: [] })).toBe(true);
    expect(
      isBackgroundRequest({
        type: 'vcc:check-hosts',
        origins: ['https://cdn.example.com'],
      }),
    ).toBe(true);
    expect(isBackgroundRequest({ type: 'vcc:check-hosts', origins: ['x'.repeat(201)] })).toBe(
      false,
    );
    expect(
      isBackgroundRequest({ type: 'vcc:check-hosts', origins: Array(21).fill('https://a.b') }),
    ).toBe(false);
  });

  it('validates operator review payloads on start-import', () => {
    const base = { type: 'vcc:start-import', tabId: 7 };
    expect(
      isBackgroundRequest({
        ...base,
        overrides: { year: 2021, make: 'Toyota' },
        photoUrls: ['https://cdn.example/a.jpg'],
      }),
    ).toBe(true);
    expect(
      isBackgroundRequest({
        ...base,
        manualEntry: {
          title: 't',
          year: null,
          make: '',
          model: '',
          trim: '',
          description: '',
        },
        userFiles: [{ name: 'a.jpg', dataUrl: 'data:image/jpeg;base64,AAAA' }],
      }),
    ).toBe(true);
    // Unknown override keys, non-image uploads, and oversized photo
    // selections are rejected: claims/photos can never be typed in.
    expect(isBackgroundRequest({ ...base, overrides: { vin: 'X' } })).toBe(false);
    expect(isBackgroundRequest({ ...base, overrides: { year: 2021.5 } })).toBe(false);
    expect(
      isBackgroundRequest({
        ...base,
        userFiles: [{ name: 'a.txt', dataUrl: 'data:text/plain;base64,AAAA' }],
      }),
    ).toBe(false);
    expect(isBackgroundRequest({ ...base, photoUrls: Array(41).fill('https://a.b/x.jpg') })).toBe(
      false,
    );
    expect(isBackgroundRequest({ ...base, manualEntry: { title: '' } })).toBe(false);
  });

  it('validates the shared listing shape', () => {
    expect(isExtractedListing(listing)).toBe(true);
    expect(isExtractedListing(null)).toBe(false);
    expect(isExtractedListing({ ...listing, year: '2020' })).toBe(false);
  });

  it('validates override, manual-entry, and file shapes directly', () => {
    expect(isListingOverrides({ year: 2021, trim: null })).toBe(false);
    expect(isListingOverrides({ year: 2021 })).toBe(true);
    expect(isListingOverrides({ title: 'x'.repeat(501) })).toBe(false);
    expect(
      isManualEntry({ title: 't', year: null, make: '', model: '', trim: '', description: '' }),
    ).toBe(true);
    expect(isManualEntry({ title: '', year: null, make: '', model: '', trim: '' })).toBe(false);
    expect(isUserSuppliedFile({ name: 'a.jpg', dataUrl: 'data:image/png;base64,AAAA' })).toBe(true);
    expect(isUserSuppliedFile({ name: 'a.jpg', dataUrl: 'data:image/png;base64,' })).toBe(true);
    expect(isUserSuppliedFile({ name: '', dataUrl: 'data:image/png;base64,AAAA' })).toBe(false);
  });

  it('detects credential fields in message payloads', () => {
    expect(containsForbiddenKeys({ type: 'vcc:extract-result', listing })).toBe(false);
    expect(containsForbiddenKeys({ extensionToken: 'abc' })).toBe(true);
    expect(containsForbiddenKeys({ nested: [{ pairingToken: 'x' }] })).toBe(true);
    expect(containsForbiddenKeys({ listing: { ...listing, token: 'x' } })).toBe(true);
  });
});
