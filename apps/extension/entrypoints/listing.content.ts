import { browser } from 'wxt/browser';
import { defineContentScript } from 'wxt/utils/define-content-script';
import { extractorForUrl } from '../src/lib/extractors';

export default defineContentScript({
  // Site-neutral (ADR 003): content scripts NEVER auto-run on listing
  // sites. Only the synthetic fixture server keeps a registered script, so
  // automated tests exercise the message path. Real pages are inspected by
  // temporary user-activated injection of this same bundle via the
  // scripting API (background extractFromTab), which then answers the same
  // message below. CDN image origins stay unlisted on purpose: unpermitted
  // hosts surface as permission failures in the mother app, never silent drops.
  matches: ['http://127.0.0.1:8901/*'],
  main() {
    browser.runtime.onMessage.addListener((message) => {
      if (typeof message !== 'object' || message === null) return undefined;
      if ((message as { type?: unknown }).type !== 'vcc:extract-request') return undefined;
      const url = window.location.href;
      const rule = extractorForUrl(url);
      const listing = rule ? rule.extract(document, url) : null;
      // JSON-serializable metadata + image URLs only. No tokens, no bytes.
      return Promise.resolve({ type: 'vcc:extract-result', listing });
    });
  },
});
