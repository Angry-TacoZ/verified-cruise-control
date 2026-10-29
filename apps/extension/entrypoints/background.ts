import { browser } from 'wxt/browser';
import { defineBackground } from 'wxt/utils/define-background';
import { allowedContentHosts, extractorForUrl } from '../src/lib/extractors';
import { isAllowedSender, isBackgroundRequest, isContentResponse } from '../src/lib/protocol';
import { runImport } from '../src/lib/uploader';
import { getPairingToken, sessionProgressStore, setImportCancelled } from '../src/lib/store';

export default defineBackground(() => {
  // In-memory cancel flag for the active run. A suspended worker drops the
  // run with it; the popup resumes from checkpointed session progress.
  let cancelFlag = false;

  browser.runtime.onMessage.addListener(async (message, sender) => {
    const senderInfo = { tabId: sender.tab?.id, tabUrl: sender.tab?.url };
    if (!isAllowedSender(senderInfo, allowedContentHosts())) return undefined;
    if (!isBackgroundRequest(message)) return undefined;

    if (message.type === 'vcc:get-state') {
      const token = await getPairingToken();
      const progress = await sessionProgressStore().load();
      return { paired: token !== null, progress };
    }

    if (message.type === 'vcc:cancel-import') {
      cancelFlag = true;
      await setImportCancelled(true);
      return { ok: true };
    }

    if (message.type === 'vcc:start-import') {
      const token = await getPairingToken();
      if (!token) return { ok: false, error: 'extension is not paired' };
      let url: string;
      try {
        url = (await browser.tabs.get(message.tabId)).url ?? '';
      } catch {
        return { ok: false, error: 'tab is no longer available' };
      }
      if (!extractorForUrl(url)) return { ok: false, error: 'no extractor for this page' };
      let response: unknown;
      try {
        response = await browser.tabs.sendMessage(message.tabId, {
          type: 'vcc:extract-request',
        });
      } catch {
        return { ok: false, error: 'content script unreachable (missing host permission?)' };
      }
      if (!isContentResponse(response) || response.type !== 'vcc:extract-result') {
        return { ok: false, error: 'invalid extractor response' };
      }
      if (!response.listing) return { ok: false, error: 'no listing detected on this page' };
      cancelFlag = false;
      await setImportCancelled(false);
      try {
        const progress = await runImport(token, response.listing, {
          fetchImpl: fetch,
          store: sessionProgressStore(),
          concurrency: 3,
          maxAttempts: 2,
          isCancelled: () => cancelFlag,
        });
        return { ok: true, progress };
      } catch (err) {
        return { ok: false, error: err instanceof Error ? err.message : 'import failed' };
      }
    }

    return undefined;
  });
});
