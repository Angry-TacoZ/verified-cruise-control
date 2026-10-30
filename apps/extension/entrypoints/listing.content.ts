import { browser } from 'wxt/browser';
import { defineContentScript } from 'wxt/utils/define-content-script';
import { extractorForUrl } from '../src/lib/extractors';

export default defineContentScript({
  // Synthetic fixture server first; dealership hosts join as optional
  // permissions with dedicated extractor rules in later PRs.
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
