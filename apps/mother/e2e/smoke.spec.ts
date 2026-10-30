/** Mother-app smoke: setup → manual import → gallery → screenshots.
 *
 * Drives the real Vite dev server + Django backend (see playwright.config.ts).
 * Screenshots land in docs/demo/ for the PR description.
 */
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { mintSetupToken, repoRoot } from './helpers';

const demoDir = path.join(repoRoot, 'docs', 'demo');
const fixturePhoto = path.join(repoRoot, 'fixtures', 'synthetic-listings', 'photos', 'front.jpg');

test('operator setup, manual import, and photo gallery', async ({ page }) => {
  const title = `E2E Test Coupe — Playwright smoke ${Date.now()}`;
  // 1. Setup with a freshly minted token (works for bootstrap and re-entry).
  await page.goto('/#/setup');
  await page.getByLabel('Setup token').fill(mintSetupToken());
  await page.getByRole('button', { name: /Sign in/ }).click();
  await expect(
    page.getByText(/signed in as the local operator|Extension pairing token/),
  ).toBeVisible({
    timeout: 15_000,
  });

  // 2. Pairing page shows the active initial pairing.
  await page.goto('/#/pairing');
  await expect(page.getByText('active').first()).toBeVisible({ timeout: 15_000 });

  // 3. Manual import with a real fixture photo.
  await page.goto('/#/import');
  await page.getByLabel('Source URL *').fill('http://127.0.0.1:8901/listing-1987-coupe.html');
  await page.getByLabel('Source site *').fill('example-motors.test');
  await page.getByLabel('Title *').fill(title);
  await page.getByLabel('Year').fill('1987');
  await page.getByLabel('Make').fill('Test');
  await page.getByLabel('Model').fill('Coupe');
  await page
    .getByLabel('Original description')
    .fill('Imported by the automated Playwright smoke test.');
  await page.getByLabel(/Advertised claims/).fill('cruise control\nlane keeping assist');
  await page.getByLabel(/Photographs/).setInputFiles([fixturePhoto]);
  await page.getByRole('button', { name: /Create listing/ }).click();
  const openLink = page.getByRole('link', { name: /Open the imported listing/ });
  await expect(openLink).toBeVisible({ timeout: 30_000 });

  // 4. Detail page renders the real gallery image.
  await openLink.click();
  await expect(page.getByText(title)).toBeVisible();
  const galleryImage = page.getByAltText(/Original listing photo/);
  await expect(galleryImage).toBeVisible({ timeout: 15_000 });
  await expect(galleryImage).toHaveAttribute('src', /\/api\/listings\/.+\/photos\/.+/);
  await page.screenshot({ path: path.join(demoDir, 'listing-detail.png') });

  // 5. Dashboard lists the import with backend-connected status.
  await page.goto('/#/');
  await expect(page.getByText('backend connected')).toBeVisible();
  await expect(page.getByText(title)).toBeVisible();
  await page.screenshot({ path: path.join(demoDir, 'dashboard.png') });
});
