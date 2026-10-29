/** Real extension E2E: load the built MV3 artifact in Chromium, pair it
 * through the real popup UI, and import the synthetic fixture listing
 * through the real content script + background worker.
 *
 * Local-only (needs a headed browser for --load-extension); not run in CI.
 * Build the extension first (`npm run build --prefix apps/extension`),
 * with VITE_VCC_API_BASE / VCC_API_HOST set when the API port is overridden.
 */
import path from 'node:path';
import { chromium, expect, test } from '@playwright/test';
import { FIXTURE, MOTHER, PARTIAL_FIXTURE, mintSetupToken, repoRoot } from './helpers';

const extensionPath = path.join(repoRoot, 'apps', 'extension', '.output', 'chrome-mv3');
const demoDir = path.join(repoRoot, 'docs', 'demo');

test('extension imports the synthetic listing end to end', async () => {
  const context = await chromium.launchPersistentContext('', {
    headless: false,
    args: [
      `--disable-extensions-except=${extensionPath}`,
      `--load-extension=${extensionPath}`,
      '--no-first-run',
      '--no-default-browser-check',
    ],
  });
  try {
    // 1. Service worker comes online.
    let worker = context.serviceWorkers()[0];
    if (!worker) worker = await context.waitForEvent('serviceworker', { timeout: 30_000 });
    const extensionId = worker.url().split('/')[2];
    expect(extensionId).toBeTruthy();

    // 2. Sign in to the mother app and mint a pairing token via rotate.
    const page = await context.newPage();
    await page.goto(`${MOTHER}/#/setup`);
    await page.getByLabel('Setup token').fill(mintSetupToken());
    await page.getByRole('button', { name: /Sign in/ }).click();
    await expect(
      page.getByText(/signed in as the local operator|Extension pairing token/),
    ).toBeVisible({
      timeout: 15_000,
    });
    await page.goto(`${MOTHER}/#/pairing`);
    await page.getByRole('button', { name: /Revoke all and issue new token/ }).click();
    const tokenCode = page.locator('div.bg-amber-50 code').first();
    await expect(tokenCode).toBeVisible({ timeout: 15_000 });
    const pairingToken = (await tokenCode.textContent())?.trim() ?? '';
    expect(pairingToken.length).toBeGreaterThan(20);

    // 3. Pair through the real popup UI (rendered in a tab for automation).
    const popup = await context.newPage();
    await popup.goto(`chrome-extension://${extensionId}/popup.html`);
    await expect(popup.getByText('Verified Cruise Control')).toBeVisible();
    await popup.getByLabel(/Pairing token/).fill(pairingToken);
    await popup.getByRole('button', { name: 'Pair extension' }).click();
    await expect(popup.getByText('paired').first()).toBeVisible({ timeout: 15_000 });
    await popup.screenshot({ path: path.join(demoDir, 'extension-popup.png') });

    // 4. Content script extracts the fixture page in the real browser.
    const listing = await context.newPage();
    await listing.goto(FIXTURE);
    await expect(listing.locator('[data-vcc-listing]')).toBeAttached();
    const tabs = await popup.evaluate(async (fixture: string) => {
      const found = await chrome.tabs.query({ url: fixture });
      return found.map((t) => t.id);
    }, FIXTURE);
    expect(tabs.length).toBeGreaterThan(0);
    // Manifest content scripts inject at document_idle; poll until the tab
    // answers instead of racing it.
    let extracted: {
      type: string;
      listing: { title: string; photos: unknown[] };
    } | null = null;
    const deadline = Date.now() + 15_000;
    for (;;) {
      try {
        extracted = await popup.evaluate(async (tabId: number) => {
          return await chrome.tabs.sendMessage(tabId, { type: 'vcc:extract-request' });
        }, tabs[0] as number);
        break;
      } catch (err) {
        if (Date.now() > deadline) throw err;
        await new Promise((r) => setTimeout(r, 500));
      }
    }
    expect(extracted!.type).toBe('vcc:extract-result');
    expect(extracted!.listing.title).toContain('synthetic fixture');
    expect(extracted!.listing.photos).toHaveLength(3);

    // 5. Background worker imports: real downloads + authenticated uploads.
    // Sent from the popup page — the production path (a worker does not
    // receive its own runtime messages).
    const result = await popup.evaluate(async (tabId: number) => {
      return await chrome.runtime.sendMessage({ type: 'vcc:start-import', tabId });
    }, tabs[0] as number);
    expect(result.ok).toBe(true);
    expect(result.progress.status).toBe('complete');
    expect(result.progress.listingId).toBeTruthy();
    expect(result.progress.photos.map((p: { status: string }) => p.status)).toEqual([
      'done',
      'done',
      'done',
    ]);

    // 6. The import is visible in the mother app with real photos.
    await page.goto(`${MOTHER}/#/listings/${result.progress.listingId}`);
    await expect(page.getByText('1987 Test Coupe LX — synthetic fixture')).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByAltText(/Original listing photo/).first()).toBeVisible({
      timeout: 15_000,
    });

    // 7. Retry-safety: re-running the import leaves the original listing's
    // evidence untouched (exactly 3 photo rows — no duplicates). Per-listing
    // hash dedupe of the retry path itself is covered by backend unit tests.
    const again = await popup.evaluate(async (tabId: number) => {
      return await chrome.runtime.sendMessage({ type: 'vcc:start-import', tabId });
    }, tabs[0] as number);
    expect(again.ok).toBe(true);
    const photosRes = await page.request.get(
      `${MOTHER}/api/listings/${result.progress.listingId}/photos`,
    );
    expect(photosRes.ok()).toBe(true);
    expect((await photosRes.json()) as unknown[]).toHaveLength(3);
    await page.goto(`${MOTHER}/#/listings/${result.progress.listingId}`);
    await expect(page.getByText('Original photographs (3)')).toBeVisible();

    // 8. Partial import: the partial fixture has one photo URL that 404s.
    const before = (await (await page.request.get(`${MOTHER}/api/listings`)).json()) as {
      count: number;
    };
    const partial = await context.newPage();
    await partial.goto(PARTIAL_FIXTURE);
    await expect(partial.locator('[data-vcc-listing]')).toBeAttached();
    const partialTabs = await popup.evaluate(async (fixture: string) => {
      const found = await chrome.tabs.query({ url: fixture });
      return found.map((t) => t.id);
    }, PARTIAL_FIXTURE);
    expect(partialTabs.length).toBeGreaterThan(0);
    const partialExtract = await popup.evaluate(async (tabId: number) => {
      return await chrome.tabs.sendMessage(tabId, { type: 'vcc:extract-request' });
    }, partialTabs[0] as number);
    expect(partialExtract.listing.photos).toHaveLength(3);
    const partialResult = await popup.evaluate(async (tabId: number) => {
      return await chrome.runtime.sendMessage({ type: 'vcc:start-import', tabId });
    }, partialTabs[0] as number);
    expect(partialResult.ok).toBe(true);
    expect(partialResult.progress.status).toBe('partial');
    expect(partialResult.progress.photos.map((p: { status: string }) => p.status)).toEqual([
      'done',
      'done',
      'failed',
    ]);
    const partialId = partialResult.progress.listingId as string;
    expect(partialId).toBeTruthy();
    // The backend agrees: one unresolved download failure means partial.
    const partialListing = (await (
      await page.request.get(`${MOTHER}/api/listings/${partialId}`)
    ).json()) as { import_status: string; failed_upload_count: number; photo_count: number };
    expect(partialListing.import_status).toBe('partial');
    expect(partialListing.failed_upload_count).toBe(1);
    expect(partialListing.photo_count).toBe(2);

    // 9. The failure is recorded server-side and shown in the mother app.
    const failuresRes = await page.request.get(`${MOTHER}/api/listings/${partialId}/failed-photos`);
    expect(failuresRes.ok()).toBe(true);
    const failures = (await failuresRes.json()) as {
      kind: string;
      resolved: boolean;
      source_url: string;
    }[];
    expect(failures).toHaveLength(1);
    expect(failures[0].kind).toBe('download');
    expect(failures[0].resolved).toBe(false);
    expect(failures[0].source_url).toContain('missing.jpg');
    await page.goto(`${MOTHER}/#/listings/${partialId}`);
    await expect(page.getByText('Import failures (1)')).toBeVisible();
    await expect(page.getByText('download failed', { exact: true })).toBeVisible();
    await page.screenshot({ path: path.join(demoDir, 'import-partial.png') });

    // 10. Retry reuses the same listing id through the real popup UI button.
    // The missing photo still 404s, so the run stays partial — and creates
    // neither a new listing nor duplicate evidence. The finished text from
    // step 8 is already on screen, so completion is detected through the
    // stored checkpoint (status flips active -> partial with a fresh
    // timestamp), not through text that is visible before the retry lands.
    await popup.goto(`chrome-extension://${extensionId}/popup.html`);
    const beforeRetry = (await popup.evaluate(async () => {
      return await chrome.runtime.sendMessage({ type: 'vcc:get-state' });
    })) as { progress: { updatedAt: number } };
    await popup.getByRole('button', { name: /Retry 1 failed photo/ }).click();
    await expect
      .poll(
        async () => {
          const state = (await popup.evaluate(async () => {
            return await chrome.runtime.sendMessage({ type: 'vcc:get-state' });
          })) as { progress: { status: string; updatedAt: number } };
          return state.progress.status === 'partial' &&
            state.progress.updatedAt !== beforeRetry.progress.updatedAt
            ? 'retried'
            : 'running';
        },
        { timeout: 30_000 },
      )
      .toBe('retried');
    await expect(popup.getByText(/Import partially complete/)).toBeVisible();
    const retried = await popup.evaluate(async () => {
      return await chrome.runtime.sendMessage({ type: 'vcc:get-state' });
    });
    expect(retried.progress.listingId).toBe(partialId);
    expect(retried.progress.status).toBe('partial');
    const partialPhotosRes = await page.request.get(`${MOTHER}/api/listings/${partialId}/photos`);
    expect(partialPhotosRes.ok()).toBe(true);
    expect((await partialPhotosRes.json()) as unknown[]).toHaveLength(2);
    const after = (await (await page.request.get(`${MOTHER}/api/listings`)).json()) as {
      count: number;
    };
    expect(after.count).toBe(before.count + 1);

    // 11. Credential storage is restricted to trusted extension contexts.
    // The worker set `storage.local` to TRUSTED_CONTEXTS at startup
    // (Chrome exposes setAccessLevel with no getter, so the effect is
    // verified behaviorally): code running with content-script privileges
    // in the fixture tab must not retrieve the pairing token through the
    // extension storage API. The probe executes in the tab's non-default
    // isolated worlds via CDP — the same privilege level as a content
    // script — with no extra extension permissions involved.
    const setLevelPresent = await worker.evaluate(() => {
      const api = globalThis as unknown as {
        chrome?: { storage?: { local?: { setAccessLevel?: unknown } } };
      };
      return typeof api.chrome?.storage?.local?.setAccessLevel;
    });
    expect(setLevelPresent).toBe('function');
    const cdp = await context.newCDPSession(partial);
    const isolatedIds: number[] = [];
    cdp.on('Runtime.executionContextCreated', (event) => {
      const ctx = event.context as { id: number; auxData?: { isDefault?: boolean } };
      if (ctx.auxData && ctx.auxData.isDefault === false) isolatedIds.push(ctx.id);
    });
    await cdp.send('Runtime.enable');
    await expect.poll(() => isolatedIds.length, { timeout: 15_000 }).toBeGreaterThan(0);
    for (const worldId of isolatedIds) {
      const evaluation = (await cdp.send('Runtime.evaluate', {
        expression: `(async () => {
          try {
            return JSON.stringify(await chrome.storage.local.get('pairingToken'));
          } catch (err) {
            return 'threw:' + (err instanceof Error ? err.message : String(err));
          }
        })()`,
        contextId: worldId,
        awaitPromise: true,
        returnByValue: true,
      })) as { result?: { value?: unknown } };
      // Either Chrome refuses the read (throw) or it resolves without the
      // credential. Seeing the real token here fails the run.
      expect(JSON.stringify(evaluation.result?.value ?? null)).not.toContain(pairingToken);
    }
    await cdp.detach();
    // Web content cannot reach extension storage APIs: Chromium exposes a
    // stub window.chrome in every page, but chrome.storage is absent there.
    expect(await listing.evaluate(() => typeof chrome.storage)).toBe('undefined');
  } finally {
    await context.close();
  }
});
