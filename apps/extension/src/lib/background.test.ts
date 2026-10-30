import { beforeEach, describe, expect, it, vi } from 'vitest';

const {
  mainHolder,
  tabsGet,
  tabsSend,
  runImportMock,
  retryMock,
  abandonMock,
  setCancelledMock,
  storeState,
} = vi.hoisted(() => ({
  mainHolder: { fn: null as ((message: unknown, sender: unknown) => Promise<unknown>) | null },
  tabsGet: vi.fn(),
  tabsSend: vi.fn(),
  runImportMock: vi.fn(),
  retryMock: vi.fn(),
  abandonMock: vi.fn(),
  setCancelledMock: vi.fn(),
  storeState: {
    token: 'tok' as string | null,
    progress: null as unknown,
  },
}));

vi.mock('wxt/browser', () => ({
  browser: {
    tabs: {
      get: (...args: unknown[]) => tabsGet(...args),
      sendMessage: (...args: unknown[]) => tabsSend(...args),
    },
    runtime: {
      onMessage: {
        addListener: (fn: (message: unknown, sender: unknown) => Promise<unknown>) => {
          mainHolder.fn = fn;
        },
      },
    },
    storage: { local: {}, session: {} },
  },
}));

vi.mock('wxt/utils/define-background', () => ({
  // Simulate worker startup: run main immediately so the message listener
  // is registered, mirroring what the worker runtime does.
  defineBackground: (main: () => void) => {
    main();
    return {};
  },
}));

vi.mock('./api', () => ({
  reportFailure: vi.fn(async () => ({})),
}));

vi.mock('./uploader', () => ({
  runImport: (...args: unknown[]) => runImportMock(...args),
  retryFailedPhotos: (...args: unknown[]) => retryMock(...args),
  abandonImport: (...args: unknown[]) => abandonMock(...args),
}));

vi.mock('./store', () => ({
  getPairingToken: async () => storeState.token,
  setPairingToken: async (token: string | null) => {
    storeState.token = token;
  },
  sessionProgressStore: () => ({
    load: async () => storeState.progress,
    save: async (progress: unknown) => {
      storeState.progress = progress;
    },
    clear: async () => {
      storeState.progress = null;
    },
  }),
  setImportCancelled: (...args: unknown[]) => setCancelledMock(...args),
  isImportCancelled: async () => false,
  ensureTrustedStorage: async () => {},
}));

type Listener = (message: unknown, sender: unknown) => Promise<unknown>;

let listener!: Listener;

function validListing() {
  return {
    sourceUrl: 'http://127.0.0.1:8901/listing-1987-coupe.html',
    sourceSite: 'example.test',
    title: 't',
    year: 1987,
    make: 'Test',
    model: 'Coupe',
    trim: '',
    vin: null,
    description: '',
    advertisedClaims: [],
    photos: [{ url: 'http://127.0.0.1:8901/photos/front.jpg' }],
    extractorId: 'synthetic-v1',
  };
}

beforeEach(async () => {
  vi.clearAllMocks();
  storeState.token = 'tok';
  storeState.progress = null;
  tabsGet.mockResolvedValue({ url: 'http://127.0.0.1:8901/listing-1987-coupe.html' });
  tabsSend.mockResolvedValue({ type: 'vcc:extract-result', listing: validListing() });
  // The entrypoint module executes once (worker startup); later imports are
  // cached, so the listener is read from shared holder state instead of
  // asserting a fresh registration each time.
  await import('../../entrypoints/background');
  expect(mainHolder.fn).not.toBeNull();
  listener = mainHolder.fn as Listener;
});

describe('background single-active-operation guard', () => {
  it('rejects a second start while one import runs, then releases on completion', async () => {
    let release!: (value: unknown) => void;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    runImportMock.mockReturnValueOnce(gate);

    const first = listener({ type: 'vcc:start-import', tabId: 7 }, {});
    const second = listener({ type: 'vcc:start-import', tabId: 7 }, {});
    await expect(second).resolves.toMatchObject({
      ok: false,
      error: expect.stringMatching(/already running/),
    });
    expect(runImportMock).toHaveBeenCalledTimes(1);

    release({ status: 'complete' });
    await expect(first).resolves.toMatchObject({ ok: true });

    // Completion released the guard: a later import proceeds normally.
    runImportMock.mockResolvedValueOnce({ status: 'complete' });
    await expect(listener({ type: 'vcc:start-import', tabId: 7 }, {})).resolves.toMatchObject({
      ok: true,
    });
    expect(runImportMock).toHaveBeenCalledTimes(2);
  });

  it('rejects a retry while an import runs', async () => {
    let release!: (value: unknown) => void;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    runImportMock.mockReturnValueOnce(gate);

    const first = listener({ type: 'vcc:start-import', tabId: 7 }, {});
    await expect(listener({ type: 'vcc:retry-failed' }, {})).resolves.toMatchObject({
      ok: false,
      error: expect.stringMatching(/already running/),
    });
    expect(retryMock).not.toHaveBeenCalled();

    release({ status: 'partial' });
    await expect(first).resolves.toMatchObject({ ok: true });
  });

  it('rejects abandonment while an import runs', async () => {
    let release!: (value: unknown) => void;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    runImportMock.mockReturnValueOnce(gate);

    const first = listener({ type: 'vcc:start-import', tabId: 7 }, {});
    await expect(listener({ type: 'vcc:abandon-import' }, {})).resolves.toMatchObject({
      ok: false,
      error: expect.stringMatching(/cancel it first/),
    });
    expect(abandonMock).not.toHaveBeenCalled();

    release({ status: 'partial' });
    await expect(first).resolves.toMatchObject({ ok: true });
  });

  it('abandons an idle checkpoint and clears the cancel flag', async () => {
    abandonMock.mockResolvedValueOnce({ status: 'failed', listingId: 'L1' });
    await expect(listener({ type: 'vcc:abandon-import' }, {})).resolves.toMatchObject({
      ok: true,
    });
    expect(abandonMock).toHaveBeenCalledTimes(1);
    expect(setCancelledMock).toHaveBeenCalledWith(false);
  });

  it('cancel during a live import flags cancellation; cancel while idle abandons', async () => {
    let release!: (value: unknown) => void;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    let capturedDeps: { isCancelled?: () => boolean } = {};
    runImportMock.mockImplementationOnce(
      (_token: unknown, _listing: unknown, deps: { isCancelled?: () => boolean }) => {
        capturedDeps = deps;
        return gate;
      },
    );

    const first = listener({ type: 'vcc:start-import', tabId: 7 }, {});
    // Wait until the run has actually begun (gate still pending, guard
    // held) before cancelling: this is the live-operation case.
    await vi.waitFor(() => expect(runImportMock).toHaveBeenCalledTimes(1));
    await expect(listener({ type: 'vcc:cancel-import' }, {})).resolves.toMatchObject({
      ok: true,
    });
    // Live path: the running operation is flag-cancelled, not abandoned.
    expect(capturedDeps.isCancelled?.()).toBe(true);
    expect(abandonMock).not.toHaveBeenCalled();
    expect(setCancelledMock).toHaveBeenCalledWith(true);

    release({ status: 'partial' });
    await expect(first).resolves.toMatchObject({ ok: true });

    // Idle path: no operation in flight, so cancel abandons the checkpoint.
    abandonMock.mockResolvedValueOnce({ status: 'failed', listingId: 'L1' });
    await expect(listener({ type: 'vcc:cancel-import' }, {})).resolves.toMatchObject({
      ok: true,
    });
    expect(abandonMock).toHaveBeenCalledTimes(1);
  });
});
