import { beforeEach, describe, expect, it, vi } from 'vitest';

const getItem = vi.fn();
const setItem = vi.fn();

vi.mock('wxt/utils/storage', () => ({
  storage: {
    getItem: (...args: unknown[]) => getItem(...args),
    setItem: (...args: unknown[]) => setItem(...args),
  },
}));

type ChromeGlobal = { chrome?: { storage?: { local?: unknown } } };

function setLocalArea(area: unknown) {
  (globalThis as unknown as ChromeGlobal).chrome =
    area === undefined ? undefined : { storage: { local: area } };
}

/** Fresh module state per test: the restriction promise is memoized. */
async function loadStore() {
  vi.resetModules();
  return import('./store');
}

beforeEach(() => {
  getItem.mockReset();
  setItem.mockReset();
  setLocalArea(undefined);
});

describe('ensureTrustedStorage (fail-closed)', () => {
  it('rejects when the browser has no storage access-level API', async () => {
    setLocalArea({});
    const store = await loadStore();
    await expect(store.ensureTrustedStorage()).rejects.toMatchObject({
      name: 'StorageRestrictionError',
    });
  });

  it('never reads the pairing token when the restriction is unavailable', async () => {
    setLocalArea({});
    const store = await loadStore();
    getItem.mockResolvedValue('tok');
    await expect(store.getPairingToken()).rejects.toMatchObject({
      name: 'StorageRestrictionError',
    });
    expect(getItem).not.toHaveBeenCalled();
    await expect(store.setPairingToken('tok')).rejects.toMatchObject({
      name: 'StorageRestrictionError',
    });
    expect(setItem).not.toHaveBeenCalled();
  });

  it('rejects when the browser refuses the restriction', async () => {
    setLocalArea({
      setAccessLevel: async () => {
        throw new Error('denied');
      },
    });
    const store = await loadStore();
    await expect(store.ensureTrustedStorage()).rejects.toThrow('denied');
    await expect(store.getPairingToken()).rejects.toThrow('denied');
    expect(getItem).not.toHaveBeenCalled();
  });

  it('restricts to trusted contexts before token access on a supported browser', async () => {
    const setAccessLevel = vi.fn(async () => {});
    setLocalArea({ setAccessLevel });
    getItem.mockResolvedValue('tok');
    const store = await loadStore();
    await store.ensureTrustedStorage();
    expect(setAccessLevel).toHaveBeenCalledWith({ accessLevel: 'TRUSTED_CONTEXTS' });
    await expect(store.getPairingToken()).resolves.toBe('tok');
    await store.setPairingToken('tok');
    expect(setItem).toHaveBeenCalled();
  });

  it('explains the incompatibility without exposing the credential', async () => {
    setLocalArea({});
    const store = await loadStore();
    const failure = await store.ensureTrustedStorage().then(
      () => null,
      (err: unknown) => err,
    );
    expect(failure).toBeInstanceOf(Error);
    expect((failure as Error).message).toContain('setAccessLevel');
    const canary = 'secret-canary-456';
    await expect(store.setPairingToken(canary)).rejects.toMatchObject({
      name: 'StorageRestrictionError',
    });
    expect((failure as Error).message).not.toContain(canary);
  });
});
