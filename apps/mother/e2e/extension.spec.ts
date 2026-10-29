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
import { FIXTURE, MOTHER, mintSetupToken, repoRoot } from './helpers';

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
    expect(result.progress.finished).toBe(true);
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
  } finally {
    await context.close();
  }
});
