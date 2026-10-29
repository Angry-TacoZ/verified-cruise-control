/** Trusted-context storage helpers (background + popup/options only).
 *
 * The content script must never import this module: it never sees the
 * pairing token. A unit test asserts that boundary statically, and the
 * `local` storage area is restricted to trusted contexts (see
 * `ensureTrustedStorage`) so a content-script context cannot read the area
 * even if it guessed the key.
 */

import { storage } from 'wxt/utils/storage';
import type { ImportProgress, ProgressStore } from './uploader';

const TOKEN_KEY = 'local:pairingToken' as const;
const PROGRESS_KEY = 'session:importProgress' as const;
const CANCEL_KEY = 'session:importCancelled' as const;

/** Minimal structural type for the storage access-level API. `@types/chrome`
 * does not declare `setAccessLevel`, so this avoids depending on untyped
 * globals while keeping the call type-checked. (Chrome exposes
 * `setAccessLevel` with no corresponding getter; the enforced effect is
 * verified behaviorally by the extension E2E, not by querying the level.) */
interface StorageAreaWithAccessLevel {
  setAccessLevel?: (options: { accessLevel: 'TRUSTED_CONTEXTS' }) => Promise<void>;
}

function localArea(): StorageAreaWithAccessLevel | null {
  const chromeApi = (globalThis as unknown as { chrome?: typeof chrome }).chrome;
  const area = chromeApi?.storage?.local as StorageAreaWithAccessLevel | undefined;
  return area ?? null;
}

let restrictionReady: Promise<void> | null = null;

/** Restrict `storage.local` to trusted extension contexts (the background
 * worker and extension pages). Content scripts run in untrusted contexts, so
 * after this resolves they can no longer read the pairing-token area. The
 * call is idempotent; on Chromium builds without the API it resolves without
 * doing anything (the module-import boundary remains the backstop, and the
 * browser-level E2E asserts the live access level). */
export function ensureTrustedStorage(): Promise<void> {
  if (!restrictionReady) {
    restrictionReady = (async () => {
      try {
        await localArea()?.setAccessLevel?.({ accessLevel: 'TRUSTED_CONTEXTS' });
      } catch {
        // Older Chromium without the API: leave the default level in place.
      }
    })();
  }
  return restrictionReady;
}

export async function getPairingToken(): Promise<string | null> {
  await ensureTrustedStorage();
  return storage.getItem<string>(TOKEN_KEY);
}

export async function setPairingToken(token: string | null): Promise<void> {
  await ensureTrustedStorage();
  await storage.setItem(TOKEN_KEY, token);
}

export function sessionProgressStore(): ProgressStore {
  return {
    load: () => storage.getItem<ImportProgress>(PROGRESS_KEY),
    save: (progress) => storage.setItem(PROGRESS_KEY, progress),
    clear: () => storage.removeItem(PROGRESS_KEY),
  };
}

export async function isImportCancelled(): Promise<boolean> {
  return (await storage.getItem<boolean>(CANCEL_KEY)) ?? false;
}

export async function setImportCancelled(value: boolean): Promise<void> {
  await storage.setItem(CANCEL_KEY, value);
}
