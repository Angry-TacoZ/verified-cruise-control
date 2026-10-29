/** Trusted-context storage helpers (background + popup/options only).
 *
 * The content script must never import this module: it never sees the
 * pairing token. A unit test asserts that boundary statically.
 */

import { storage } from 'wxt/utils/storage';
import type { ImportProgress, ProgressStore } from './uploader';

const TOKEN_KEY = 'local:pairingToken' as const;
const PROGRESS_KEY = 'session:importProgress' as const;
const CANCEL_KEY = 'session:importCancelled' as const;

export async function getPairingToken(): Promise<string | null> {
  return storage.getItem<string>(TOKEN_KEY);
}

export async function setPairingToken(token: string | null): Promise<void> {
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
