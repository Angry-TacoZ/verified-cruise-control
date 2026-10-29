/** Playwright global setup: start each run with a fresh isolated backend DB,
 * unless servers are already running (reuseExistingServer mode), in which
 * case the live database is left untouched.
 */
import fs from 'node:fs';
import { API, e2eRuntimeDir } from './helpers';

async function apiAlreadyUp(): Promise<boolean> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 2000);
    try {
      const res = await fetch(`${API}/api/health`, { signal: controller.signal });
      return res.ok;
    } finally {
      clearTimeout(timer);
    }
  } catch {
    return false;
  }
}

export default async function globalSetup(): Promise<void> {
  if (await apiAlreadyUp()) {
    console.log('E2E: API already running, keeping its database.');
    return;
  }
  fs.rmSync(e2eRuntimeDir, { recursive: true, force: true });
  console.log('E2E: wiped isolated runtime dir for a clean run.');
}
