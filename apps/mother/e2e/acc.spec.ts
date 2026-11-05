/** ACC audit path E2E: Verify-ACC import → operator queue → settled PASS.
 *
 * Real Chromium + real extension + real backend on loopback. Model
 * inference is stubbed with scripted adapters ($0, no provider calls):
 * the worker, reconciliation, dashboard, and evidence panel all run for
 * real. A live-model run still needs James's explicit authorization.
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { chromium, expect, test } from '@playwright/test';
import { FIXTURE, MOTHER, e2eRuntimeDir, mintSetupToken, repoRoot } from './helpers';

const extensionPath = path.join(repoRoot, 'apps', 'extension', '.output', 'chrome-mv3');
const demoDir = path.join(repoRoot, 'docs', 'demo');
const venvPython = path.join(repoRoot, '.venv', 'Scripts', 'python.exe');

/** Settle the listing's queued job with scripted ($0) adapters. */
function settleWithFakeModels(listingId: string): string {
  const script = `import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from audits import worker as audit_worker
from audits.adas_evidence import normalize_feature
from audits.models import AuditFinding, AuditJob
from tests.test_audits import FakeMuseAdapter, FakeSamAdapter, _obs, _segment
print("E2E_LISTING_ID", os.environ.get("E2E_LISTING_ID"))
job = AuditJob.objects.filter(listing_id=os.environ["E2E_LISTING_ID"]).order_by("-created_at").first()
print("CHOSEN_JOB", job.pk if job is not None else None,
      job.listing_id if job is not None else None,
      job.status if job is not None else None,
      job.created_at if job is not None else None)
assert job is not None and job.status in ("queued", "running"), job
import re as _re
claim_ids = [c["id"] for c in job.snapshot["claims"]]
acc_ids = {c["id"] for c in job.snapshot["claims"]
           if normalize_feature(c.get("text", "")) == "adaptive_cruise_control"}
class ReqAwareMuse(FakeMuseAdapter):
    def analyze(self, request):
        # Answer whatever (claim, photo) pairs this request asks about:
        # initial batches and crop rechecks alike. Unknown claim ids would
        # fail validation, so only known snapshot claim ids are echoed.
        wanted = set(getattr(request, "photo_ids", ()))
        cids = [c for c in _re.findall("\\\\bc\\\\d+\\\\b", getattr(request, "user_text", ""))
                if c in claim_ids]
        print("MUSE_CALL photos=", sorted(wanted), "claims=", cids,
              "text=", getattr(request, "user_text", "")[:160].replace(chr(10), " | "))
        obs = []
        for pid in wanted:
            for cid in cids:
                if cid in acc_ids:
                    obs.append(_obs(pid, cid,
                        visible_object="button cluster",
                        description="cruise buttons with following-distance icon",
                        supports_localization=True, ambiguous=False,
                        needs_crop=False, needs_other_view=False,
                        control_symbol="vehicle with distance bars",
                        control_type="button", location="steering_wheel",
                        dedicated=True))
                else:
                    obs.append(_obs(pid, cid, visible_object="none"))
        self._observations = obs
        return super().analyze(request)
settled = audit_worker.run_claimed_job("e2e-acc", ReqAwareMuse(), FakeSamAdapter(records=[_segment()]))
print("SETTLED", settled.status, settled.error_code)
for f in AuditFinding.objects.filter(job=settled).order_by("claim_key"):
    print("FINDING", f.claim_key, f.claim_text, f.state)
`;
  // Plain-python execution (django.setup): manage.py shell mangles piped
  // stdin on this platform. The script lives in the OS temp dir, never in
  // the repo.
  const scriptPath = path.join(
    os.tmpdir(),
    `e2e-settle-acc-${listingId.replace(/[^a-z0-9]/gi, '')}.py`,
  );
  fs.writeFileSync(scriptPath, script, 'utf-8');
  try {
    const output = execFileSync(venvPython, [scriptPath], {
      cwd: path.join(repoRoot, 'backend'),
      env: {
        ...process.env,
        PYTHONPATH: path.join(repoRoot, 'backend'),
        VCC_RUNTIME_DIR: e2eRuntimeDir,
        VCC_INFERENCE_ENABLED: '1',
        E2E_LISTING_ID: listingId,
      },
      encoding: 'utf-8',
    });
    fs.writeFileSync(path.join(os.tmpdir(), 'e2e-acc-settle.txt'), output);
    return output;
  } finally {
    fs.rmSync(scriptPath, { force: true });
  }
}

/** Import with the operator-requested check and log the returned identity. */
async function importWithAccCheck(
  popupPage: import('@playwright/test').Page,
  tabId: number,
): Promise<{ ok: boolean; listingId: string; status: string }> {
  const result = (await popupPage.evaluate(async (id: number) => {
    return await chrome.runtime.sendMessage({
      type: 'vcc:start-import',
      tabId: id,
      requestedCheck: 'adaptive_cruise_control',
    });
  }, tabId)) as { ok: boolean; progress: { listingId: string; status: string } };
  fs.writeFileSync(
    path.join(os.tmpdir(), 'e2e-acc-import.json'),
    JSON.stringify({
      ok: result.ok,
      listingId: result.progress?.listingId,
      status: result.progress?.status,
    }),
  );
  return { ok: result.ok, listingId: result.progress?.listingId, status: result.progress?.status };
}

test('Verify ACC import reaches a PASS finding on the dashboard', async () => {
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
    let worker = context.serviceWorkers()[0];
    if (!worker) worker = await context.waitForEvent('serviceworker', { timeout: 30_000 });
    const extensionId = worker.url().split('/')[2];
    expect(extensionId).toBeTruthy();

    const page = await context.newPage();
    await page.goto(`${MOTHER}/#/setup`);
    await page.getByLabel('Setup token').fill(mintSetupToken());
    await page.getByRole('button', { name: /Sign in/ }).click();
    await expect(
      page.getByText(/signed in as the local operator|Extension pairing token/),
    ).toBeVisible({ timeout: 15_000 });
    await page.goto(`${MOTHER}/#/pairing`);
    await page.getByRole('button', { name: /Revoke all and issue new token/ }).click();
    const tokenCode = page.locator('div.bg-amber-50 code').first();
    await expect(tokenCode).toBeVisible({ timeout: 15_000 });
    const pairingToken = (await tokenCode.textContent())?.trim() ?? '';
    expect(pairingToken.length).toBeGreaterThan(20);

    const popup = await context.newPage();
    await popup.goto(`chrome-extension://${extensionId}/popup.html`);
    await popup.getByLabel(/Pairing token/).fill(pairingToken);
    await popup.getByRole('button', { name: 'Pair extension' }).click();
    await expect(popup.getByText('paired').first()).toBeVisible({ timeout: 15_000 });

    // Import with the operator-requested ACC check (the Verify ACC path).
    const listingPage = await context.newPage();
    await listingPage.goto(FIXTURE);
    await expect(listingPage.locator('[data-vcc-listing]')).toBeAttached();
    const tabs = await popup.evaluate(async (fixture: string) => {
      const found = await chrome.tabs.query({ url: fixture });
      return found.map((t) => t.id);
    }, FIXTURE);
    expect(tabs.length).toBeGreaterThan(0);
    const imported = await importWithAccCheck(popup, tabs[0] as number);
    expect(imported.ok).toBe(true);
    expect(imported.status).toBe('complete');
    const listingId = imported.listingId;
    expect(listingId).toBeTruthy();

    // Backend stores the check apart from seller claims.
    const stored = (await (
      await page.request.get(`${MOTHER}/api/listings/${listingId}`)
    ).json()) as { advertised_claims: string[]; requested_checks: string[] };
    expect(stored.requested_checks).toEqual(['adaptive_cruise_control']);
    expect(stored.advertised_claims).toContain('adaptive cruise control');
    expect(stored.advertised_claims).not.toContain('(user-requested check)');

    // Detail page: operator-requested + seller-advertised provenance, Run ACC Audit.
    await page.goto(`${MOTHER}/#/listings/${listingId}`);
    await expect(page.getByText('Seller-advertised').first()).toBeVisible({
      timeout: 15_000,
    });
    await page.getByRole('button', { name: 'Run ACC Audit' }).click();
    await expect(page.getByText('Audit queued — no ACC result yet.')).toBeVisible({
      timeout: 15_000,
    });

    // Settle the queued job with $0 scripted adapters (no provider calls).
    const output = settleWithFakeModels(listingId);
    expect(output).toContain('SETTLED complete');
    expect(output).toContain('visually_supported');
    expect(output).not.toContain('FINDING c1 cruise control visually_supported');

    // Dashboard shows the completed ACC result.
    await page.goto(`${MOTHER}/#/`);
    await expect(page.getByText('ACC: PASS').first()).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ path: path.join(demoDir, 'acc-dashboard.png') });

    // Detail shows the PASS banner, the five-state finding, and evidence.
    await page.goto(`${MOTHER}/#/listings/${listingId}`);
    const banner = page.getByText(/PASS — Visual evidence supports ACC/);
    await expect(banner).toBeVisible({ timeout: 15_000 });
    await banner.scrollIntoViewIfNeeded();
    await expect(page.getByText('visually_supported')).toBeVisible();
    await expect(page.getByAltText(/Original listing photo/).first()).toBeVisible();
    await page.screenshot({ path: path.join(demoDir, 'acc-detail.png') });
  } finally {
    await context.close();
  }
});
