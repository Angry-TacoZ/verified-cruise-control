import path from 'node:path';
import { defineConfig } from '@playwright/test';

const repoRoot = path.resolve(import.meta.dirname, '..', '..');
const venvPython = path.join(repoRoot, '.venv', 'Scripts', 'python.exe');

// Fixed loopback ports by default; override when another project squats on
// them (see README "Port conflicts"). The extension artifact must be rebuilt
// with matching VITE_VCC_API_BASE / VCC_API_HOST for non-default API ports.
const apiPort = process.env.VCC_E2E_API_PORT ?? '8000';
const webPort = process.env.VCC_E2E_WEB_PORT ?? '5173';
const fixPort = process.env.VCC_E2E_FIX_PORT ?? '8901';

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  timeout: 120_000,
  // Specs share one backend DB file; run serially for determinism.
  workers: 1,
  fullyParallel: false,
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command: `"${venvPython}" backend/manage.py migrate && "${venvPython}" backend/manage.py runserver 127.0.0.1:${apiPort} --noreload`,
      url: `http://127.0.0.1:${apiPort}/api/health`,
      cwd: repoRoot,
      timeout: 120_000,
      reuseExistingServer: !process.env.CI,
      env: { VCC_RUNTIME_DIR: path.join(repoRoot, 'backend', 'runtime-data-e2e') },
    },
    {
      command: `npx vite --host 127.0.0.1 --port ${webPort} --strictPort`,
      url: `http://127.0.0.1:${webPort}`,
      cwd: path.join(repoRoot, 'apps', 'mother'),
      timeout: 120_000,
      reuseExistingServer: !process.env.CI,
      env: { VCC_API_PROXY: `http://127.0.0.1:${apiPort}` },
    },
    {
      command: `"${venvPython}" fixtures/serve.py ${fixPort}`,
      url: `http://127.0.0.1:${fixPort}/listing-1987-coupe.html`,
      cwd: repoRoot,
      timeout: 60_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
