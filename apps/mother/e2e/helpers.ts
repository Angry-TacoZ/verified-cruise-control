/** Shared E2E helpers: env-driven loopback URLs plus setup-token minting
 * against the same isolated runtime dir the webServer backend uses.
 */
import { execFileSync } from 'node:child_process';
import path from 'node:path';

export const repoRoot = path.resolve(import.meta.dirname, '..', '..', '..');
export const e2eRuntimeDir = path.join(repoRoot, 'backend', 'runtime-data-e2e');

const apiPort = process.env.VCC_E2E_API_PORT ?? '8000';
const webPort = process.env.VCC_E2E_WEB_PORT ?? '5173';
const fixPort = process.env.VCC_E2E_FIX_PORT ?? '8901';

export const API = `http://127.0.0.1:${apiPort}`;
export const MOTHER = `http://127.0.0.1:${webPort}`;
export const FIXTURE = `http://127.0.0.1:${fixPort}/listing-1987-coupe.html`;
export const PARTIAL_FIXTURE = `http://127.0.0.1:${fixPort}/listing-partial.html`;

const venvPython = path.join(repoRoot, '.venv', 'Scripts', 'python.exe');

export function mintSetupToken(): string {
  const out = execFileSync(venvPython, ['backend/manage.py', 'setup_token', '--regenerate'], {
    cwd: repoRoot,
    env: { ...process.env, VCC_RUNTIME_DIR: e2eRuntimeDir },
    encoding: 'utf-8',
  });
  const lines = out.trim().split('\n');
  const token = lines[lines.length - 1]?.trim() ?? '';
  if (!token) throw new Error('could not mint setup token');
  return token;
}
