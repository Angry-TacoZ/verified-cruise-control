import { describe, expect, it } from 'vitest';
import {
  containsForbiddenKeys,
  isAllowedSender,
  isBackgroundRequest,
  isContentResponse,
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

  it('detects credential fields in message payloads', () => {
    expect(containsForbiddenKeys({ type: 'vcc:extract-result', listing })).toBe(false);
    expect(containsForbiddenKeys({ extensionToken: 'abc' })).toBe(true);
    expect(containsForbiddenKeys({ nested: [{ pairingToken: 'x' }] })).toBe(true);
    expect(containsForbiddenKeys({ listing: { ...listing, token: 'x' } })).toBe(true);
  });
});
