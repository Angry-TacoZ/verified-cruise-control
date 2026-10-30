import { browser } from 'wxt/browser';
import { defineBackground } from 'wxt/utils/define-background';
import { reportFailure } from '../src/lib/api';
import { allowedContentHosts, extractorForUrl } from '../src/lib/extractors';
import { isAllowedSender, isBackgroundRequest, isContentResponse } from '../src/lib/protocol';
import {
  retryFailedPhotos,
  runImport,
  type FailureReport,
  type ImportDeps,
} from '../src/lib/uploader';
import {
  ensureTrustedStorage,
  getPairingToken,
  sessionProgressStore,
  setImportCancelled,
} from '../src/lib/store';

export default defineBackground(() => {
  // Restrict storage.local to trusted contexts before any message can be
  // handled (event dispatch cannot interleave this synchronous task). Token
  // reads gate on the same promise, so if the restriction cannot be
  // established every pairing-token access rejects and pairing plus
  // authenticated imports stay disabled. The worker itself stays alive to
  // report that state; the logged message never carries the credential.
  void ensureTrustedStorage().catch((err: unknown) => {
    console.warn(
      'storage restriction failed; pairing is disabled:',
      err instanceof Error ? err.message : err,
    );
  });

  // In-memory cancel flag for the active run. A suspended worker drops the
  // run with it; the popup resumes from checkpointed session progress.
  let cancelFlag = false;

  // Single-active-operation guard for this worker lifetime. Set
  // synchronously at message entry (before the first await) so two
  // same-tick start/retry messages cannot both enter; the checkpoint-level
  // guard in runImport/retryFailedPhotos covers races across worker
  // restarts. Always released in a finally. State queries and cancel keep
  // working while an operation holds the guard.
  let operationInFlight = false;

  // Download/permission failures never reach the upload endpoint, so the
  // worker reports them explicitly against the failing run's own listing
  // id — passed in by the caller, never re-read from the shared checkpoint
  // at report time. The backend record is idempotent, and reporting never
  // fails the run.
  async function reportFailureFor(
    token: string,
    listingId: string,
    failure: FailureReport,
  ): Promise<void> {
    await reportFailure(token, listingId, failure, fetch);
  }

  function importDeps(token: string): ImportDeps {
    return {
      fetchImpl: fetch,
      store: sessionProgressStore(),
      concurrency: 3,
      maxAttempts: 2,
      isCancelled: () => cancelFlag,
      reportFailure: (listingId, failure) => reportFailureFor(token, listingId, failure),
    };
  }

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
      if (operationInFlight) {
        return { ok: false, error: 'another import operation is already running' };
      }
      operationInFlight = true;
      try {
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
          const progress = await runImport(token, response.listing, importDeps(token));
          return { ok: true, progress };
        } catch (err) {
          return { ok: false, error: err instanceof Error ? err.message : 'import failed' };
        }
      } finally {
        operationInFlight = false;
      }
    }

    if (message.type === 'vcc:retry-failed') {
      if (operationInFlight) {
        return { ok: false, error: 'another import operation is already running' };
      }
      operationInFlight = true;
      try {
        const token = await getPairingToken();
        if (!token) return { ok: false, error: 'extension is not paired' };
        cancelFlag = false;
        await setImportCancelled(false);
        try {
          const progress = await retryFailedPhotos(token, importDeps(token));
          return { ok: true, progress };
        } catch (err) {
          return { ok: false, error: err instanceof Error ? err.message : 'retry failed' };
        }
      } finally {
        operationInFlight = false;
      }
    }

    return undefined;
  });
});
