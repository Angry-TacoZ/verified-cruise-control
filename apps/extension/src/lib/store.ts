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

/** Raised when `storage.local` cannot be restricted to trusted contexts.
 * Callers treat this as fatal for pairing and authenticated imports: the
 * extension must not silently operate with content scripts able to read the
 * pairing-token area. The message names the browser incompatibility; it
 * never carries the credential. */
export class StorageRestrictionError extends Error {
  constructor(reason: string) {
    super(`pairing-token storage is unavailable: ${reason}`);
    this.name = 'StorageRestrictionError';
  }
}

const STORAGE_UNAVAILABLE_HINT =
  'this browser does not expose chrome.storage.local.setAccessLevel; ' +
  'pairing and authenticated imports stay disabled until a compatible Chromium build is used.';

/** Restrict `storage.local` to trusted extension contexts (the background
 * worker and extension pages). Content scripts run in untrusted contexts, so
 * after this resolves they can no longer read the pairing-token area. The
 * call is idempotent and fail-closed: when the browser lacks the API or
 * rejects the call, the returned promise rejects with
 * StorageRestrictionError and every pairing-token read/write rejects too, so
 * pairing and authenticated imports cannot proceed. The module-import
 * boundary and the browser-level E2E remain backstops, not substitutes. */
export function ensureTrustedStorage(): Promise<void> {
  if (!restrictionReady) {
    restrictionReady = (async () => {
      const area = localArea();
      const setAccessLevel = area?.setAccessLevel;
      if (!area || !setAccessLevel) throw new StorageRestrictionError(STORAGE_UNAVAILABLE_HINT);
      await setAccessLevel.call(area, { accessLevel: 'TRUSTED_CONTEXTS' });
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
