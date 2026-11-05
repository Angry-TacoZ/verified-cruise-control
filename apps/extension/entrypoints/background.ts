import { browser } from 'wxt/browser';
import { defineBackground } from 'wxt/utils/define-background';
import { reportFailure, uploadPhoto } from '../src/lib/api';
import { allowedContentHosts } from '../src/lib/extractors';
import {
  MAX_PHOTO_URLS,
  MAX_USER_FILES,
  isAllowedSender,
  isBackgroundRequest,
  isContentResponse,
  type ExtractedListing,
} from '../src/lib/protocol';
import {
  abandonImport,
  applyListingOverrides,
  dataUrlToBlob,
  listingFromManualEntry,
  normalizeProgress,
  retryFailedPhotos,
  runImport,
  sanitizeUserFilename,
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

  // Bundled extractor file (built from entrypoints/listing.content.ts)
  // injected on demand into tabs without a registered content script. The
  // file only registers a message listener — it extracts nothing until the
  // worker asks — so injection is inert until the operator acts.
  const INJECTED_CONTENT_SCRIPT_FILE = 'content-scripts/listing.js';

  /** Extract the listing from a tab. Tabs with an auto-registered content
   * script (the synthetic fixture server) answer directly; every other
   * page gets the same bundled extractor injected on demand via scripting
   * + activeTab, where the generic fallback runs with no per-site code.
   * Injection is temporary and user-activated: nothing runs until the
   * operator clicks, and no code is ever fetched remotely. */
  async function extractFromTab(tabId: number): Promise<ExtractedListing | null> {
    let url: string;
    try {
      url = (await browser.tabs.get(tabId)).url ?? '';
    } catch {
      throw new Error('tab is no longer available');
    }
    const askContentScript = async (): Promise<ExtractedListing | null> => {
      const response: unknown = await browser.tabs.sendMessage(tabId, {
        type: 'vcc:extract-request',
      });
      if (!isContentResponse(response) || response.type !== 'vcc:extract-result') {
        throw new Error('invalid extractor response');
      }
      return response.listing;
    };
    try {
      return await askContentScript();
    } catch (err) {
      // A response that fails validation is a real defect, not a missing
      // listener: fail closed instead of injecting over it.
      if (err instanceof Error && err.message === 'invalid extractor response') throw err;
    }
    let scheme: string;
    try {
      scheme = new URL(url).protocol;
    } catch {
      scheme = '';
    }
    if (scheme !== 'http:' && scheme !== 'https:') {
      throw new Error('only web pages can be inspected');
    }
    try {
      await browser.scripting.executeScript({
        target: { tabId },
        files: [INJECTED_CONTENT_SCRIPT_FILE],
      });
    } catch {
      throw new Error('could not inspect this page (extension scripting blocked?)');
    }
    try {
      return await askContentScript();
    } catch {
      throw new Error('page does not allow extension content scripts');
    }
  }

  /** Upload operator-selected local files as user-supplied evidence after
   * the page-photo import finishes. One-shot (outside the photo checkpoint):
   * failures surface immediately so the operator can retry or drop them. */
  async function uploadUserFiles(
    token: string,
    listingId: string,
    userFiles: { name: string; dataUrl: string }[],
  ): Promise<string | null> {
    const failures: string[] = [];
    for (const file of userFiles.slice(0, MAX_USER_FILES)) {
      try {
        const bytes = dataUrlToBlob(file.dataUrl);
        await uploadPhoto(token, listingId, bytes, sanitizeUserFilename(file.name), null, fetch, {
          userSupplied: true,
        });
      } catch (err) {
        failures.push(`${file.name}: ${err instanceof Error ? err.message : 'upload failed'}`);
      }
    }
    return failures.length > 0 ? `user-supplied uploads failed: ${failures.join('; ')}` : null;
  }

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
      return { paired: token !== null, progress, operationRunning: operationInFlight };
    }

    if (message.type === 'vcc:abandon-import') {
      // Never interrupt a genuinely active operation: abandonment is only
      // safe when this worker has nothing in flight (the checkpoint-level
      // state alone cannot prove that, so wall-clock heuristics are not
      // used). A live run must go through the cancellation workflow.
      if (operationInFlight) {
        return { ok: false, error: 'an import operation is running; cancel it first' };
      }
      operationInFlight = true;
      try {
        const progress = await abandonImport({ store: sessionProgressStore() });
        await setImportCancelled(false);
        return { ok: true, progress };
      } catch (err) {
        return { ok: false, error: err instanceof Error ? err.message : 'abandon failed' };
      } finally {
        operationInFlight = false;
      }
    }

    if (message.type === 'vcc:cancel-import') {
      if (!operationInFlight) {
        // No live run to flag-cancel. Hold the guard across the transition
        // so a simultaneous start/retry cannot interleave with it.
        operationInFlight = true;
        try {
          // A missing or already-terminal checkpoint is a genuine no-op:
          // only a live abandonment can fail here.
          const stored = normalizeProgress(await sessionProgressStore().load());
          if (!stored || !stored.listingId || stored.status !== 'active') return { ok: true };
          const progress = await abandonImport({ store: sessionProgressStore() });
          await setImportCancelled(false);
          return { ok: true, progress };
        } catch (err) {
          return { ok: false, error: err instanceof Error ? err.message : 'cancel failed' };
        } finally {
          operationInFlight = false;
        }
      }
      cancelFlag = true;
      await setImportCancelled(true);
      return { ok: true };
    }

    if (message.type === 'vcc:preview-extract') {
      // No pairing token involved: preview inspects the page and returns
      // metadata + image URLs only, never credentials or bytes.
      try {
        const listing = await extractFromTab(message.tabId);
        return { type: 'vcc:preview-result', listing };
      } catch (err) {
        return {
          type: 'vcc:error',
          message: err instanceof Error ? err.message : 'preview failed',
        };
      }
    }

    if (message.type === 'vcc:check-hosts') {
      // Report which image origins the extension may already fetch. The
      // popup requests missing origins explicitly via permissions.request.
      const access: Record<string, boolean> = {};
      for (const origin of message.origins) {
        const pattern = origin.endsWith('/*') ? origin : `${origin}/*`;
        try {
          access[origin] = await browser.permissions.contains({ origins: [pattern] });
        } catch {
          access[origin] = false;
        }
      }
      return { type: 'vcc:host-access', access };
    }

    if (message.type === 'vcc:start-import') {
      if (operationInFlight) {
        return { ok: false, error: 'another import operation is already running' };
      }
      operationInFlight = true;
      try {
        const token = await getPairingToken();
        if (!token) return { ok: false, error: 'extension is not paired' };
        let listing: ExtractedListing;
        try {
          if (message.manualEntry) {
            // Operator-entered vehicle for pages with no detectable
            // listing: identity comes from the form, photos only from
            // operator-selected local files uploaded as user-supplied.
            let pageUrl = '';
            try {
              pageUrl = (await browser.tabs.get(message.tabId)).url ?? '';
            } catch {
              return { ok: false, error: 'tab is no longer available' };
            }
            listing = listingFromManualEntry(message.manualEntry, pageUrl);
          } else {
            const extracted = await extractFromTab(message.tabId);
            if (!extracted) return { ok: false, error: 'no listing detected on this page' };
            listing = message.overrides
              ? applyListingOverrides(extracted, message.overrides)
              : extracted;
            if (message.photoUrls) {
              // Selection only: the operator picks from detected URLs and
              // can never invent photo evidence by typing a URL.
              const allowed = new Set(listing.photos.map((p) => p.url));
              listing = {
                ...listing,
                photos: message.photoUrls
                  .filter((u) => allowed.has(u))
                  .slice(0, MAX_PHOTO_URLS)
                  .map((url) => ({ url })),
              };
            }
          }
        } catch (err) {
          return { ok: false, error: err instanceof Error ? err.message : 'import failed' };
        }
        cancelFlag = false;
        await setImportCancelled(false);
        try {
          // Operator-requested check only (allowlisted in protocol
          // validation): never inferred from the page, never a seller claim.
          const progress = await runImport(
            token,
            listing,
            importDeps(token),
            message.requestedCheck ?? null,
          );
          let warning: string | undefined;
          if (message.userFiles && message.userFiles.length > 0 && progress.listingId) {
            warning =
              (await uploadUserFiles(token, progress.listingId, message.userFiles)) ?? undefined;
          }
          return warning ? { ok: true, progress, warning } : { ok: true, progress };
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
