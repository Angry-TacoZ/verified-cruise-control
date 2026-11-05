import { beforeEach, describe, expect, it, vi } from 'vitest';

const {
  mainHolder,
  tabsGet,
  tabsSend,
  scriptingExecute,
  permissionsContains,
  runImportMock,
  retryMock,
  abandonMock,
  setCancelledMock,
  loadMock,
  uploadPhotoMock,
  storeState,
} = vi.hoisted(() => ({
  mainHolder: { fn: null as ((message: unknown, sender: unknown) => Promise<unknown>) | null },
  tabsGet: vi.fn(),
  tabsSend: vi.fn(),
  scriptingExecute: vi.fn(),
  permissionsContains: vi.fn(),
  runImportMock: vi.fn(),
  retryMock: vi.fn(),
  abandonMock: vi.fn(),
  setCancelledMock: vi.fn(),
  loadMock: vi.fn(),
  uploadPhotoMock: vi.fn(),
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
    scripting: {
      executeScript: (...args: unknown[]) => scriptingExecute(...args),
    },
    permissions: {
      contains: (...args: unknown[]) => permissionsContains(...args),
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
  uploadPhoto: (...args: unknown[]) => uploadPhotoMock(...args),
}));

vi.mock('./uploader', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./uploader')>();
  return {
    ...actual,
    runImport: (...args: unknown[]) => runImportMock(...args),
    retryFailedPhotos: (...args: unknown[]) => retryMock(...args),
    abandonImport: (...args: unknown[]) => abandonMock(...args),
  };
});

vi.mock('./store', () => ({
  getPairingToken: async () => storeState.token,
  setPairingToken: async (token: string | null) => {
    storeState.token = token;
  },
  sessionProgressStore: () => ({
    load: (...args: unknown[]) => loadMock(...args),
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
  loadMock.mockImplementation(async () => storeState.progress);
  tabsGet.mockResolvedValue({ url: 'http://127.0.0.1:8901/listing-1987-coupe.html' });
  tabsSend.mockResolvedValue({ type: 'vcc:extract-result', listing: validListing() });
  scriptingExecute.mockResolvedValue([]);
  permissionsContains.mockResolvedValue(true);
  uploadPhotoMock.mockResolvedValue({ duplicate: false });
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

  it('forwards the operator-requested ACC check to runImport', async () => {
    runImportMock.mockResolvedValueOnce({ status: 'complete' });
    await expect(
      listener(
        { type: 'vcc:start-import', tabId: 7, requestedCheck: 'adaptive_cruise_control' },
        {},
      ),
    ).resolves.toMatchObject({ ok: true });
    expect(runImportMock).toHaveBeenCalledTimes(1);
    expect(runImportMock.mock.calls[0][3]).toBe('adaptive_cruise_control');
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

    // Idle path with an active checkpoint: cancel abandons it.
    storeState.progress = {
      runId: 'r1',
      listingId: 'L1',
      sourceUrl: 'http://127.0.0.1:8901/l/1',
      photos: [{ url: 'http://127.0.0.1:8901/photos/a.jpg', status: 'pending' }],
      startedAt: 1,
      updatedAt: 1,
      status: 'active',
    };
    abandonMock.mockResolvedValueOnce({ status: 'failed', listingId: 'L1' });
    await expect(listener({ type: 'vcc:cancel-import' }, {})).resolves.toMatchObject({
      ok: true,
    });
    expect(abandonMock).toHaveBeenCalledTimes(1);
  });

  it('idle cancel with no checkpoint succeeds as a no-op', async () => {
    storeState.progress = null;
    await expect(listener({ type: 'vcc:cancel-import' }, {})).resolves.toMatchObject({
      ok: true,
    });
    expect(abandonMock).not.toHaveBeenCalled();
    expect(setCancelledMock).not.toHaveBeenCalled();
  });

  it('idle cancel surfaces storage failures instead of false success', async () => {
    loadMock.mockRejectedValueOnce(new Error('db locked'));
    await expect(listener({ type: 'vcc:cancel-import' }, {})).resolves.toMatchObject({
      ok: false,
      error: expect.stringMatching(/db locked/),
    });
    expect(abandonMock).not.toHaveBeenCalled();
    // The guard was released: a later import proceeds normally.
    runImportMock.mockResolvedValueOnce({ status: 'complete' });
    await expect(listener({ type: 'vcc:start-import', tabId: 7 }, {})).resolves.toMatchObject({
      ok: true,
    });
    expect(runImportMock).toHaveBeenCalledTimes(1);
  });

  it('a start arriving during idle-cancel abandonment is rejected', async () => {
    storeState.progress = {
      runId: 'r1',
      listingId: 'L1',
      sourceUrl: 'http://127.0.0.1:8901/l/1',
      photos: [{ url: 'http://127.0.0.1:8901/photos/a.jpg', status: 'pending' }],
      startedAt: 1,
      updatedAt: 1,
      status: 'active',
    };
    let releaseAbandon!: (value: unknown) => void;
    const gate = new Promise((resolve) => {
      releaseAbandon = resolve;
    });
    abandonMock.mockReturnValueOnce(gate);
    const cancelP = listener({ type: 'vcc:cancel-import' }, {});
    // The idle cancel holds the guard while abandoning: a simultaneous
    // start cannot interleave with the transition.
    await expect(listener({ type: 'vcc:start-import', tabId: 7 }, {})).resolves.toMatchObject({
      ok: false,
      error: expect.stringMatching(/already running/),
    });
    expect(runImportMock).not.toHaveBeenCalled();
    releaseAbandon({ status: 'failed', listingId: 'L1' });
    await expect(cancelP).resolves.toMatchObject({ ok: true });
    // Guard released afterward: a later import proceeds.
    runImportMock.mockResolvedValueOnce({ status: 'complete' });
    await expect(listener({ type: 'vcc:start-import', tabId: 7 }, {})).resolves.toMatchObject({
      ok: true,
    });
    expect(runImportMock).toHaveBeenCalledTimes(1);
  });

  it('abandon handler surfaces transition failures and releases the guard', async () => {
    abandonMock.mockRejectedValueOnce(new Error('save failed'));
    await expect(listener({ type: 'vcc:abandon-import' }, {})).resolves.toMatchObject({
      ok: false,
      error: expect.stringMatching(/save failed/),
    });
    runImportMock.mockResolvedValueOnce({ status: 'complete' });
    await expect(listener({ type: 'vcc:start-import', tabId: 7 }, {})).resolves.toMatchObject({
      ok: true,
    });
  });
});

describe('background site-neutral preview and import', () => {
  it('previews a registered-host tab through its content script', async () => {
    await expect(listener({ type: 'vcc:preview-extract', tabId: 7 }, {})).resolves.toMatchObject({
      type: 'vcc:preview-result',
      listing: expect.objectContaining({ extractorId: 'synthetic-v1' }),
    });
    expect(scriptingExecute).not.toHaveBeenCalled();
  });

  it('previews an unknown site by injecting the bundled extractor', async () => {
    tabsGet.mockResolvedValueOnce({ url: 'https://dealer.example/inventory/42' });
    // No auto-registered listener on this host: the first ask fails, the
    // injected bundle answers the second.
    tabsSend.mockRejectedValueOnce(new Error('no receiving end'));
    await expect(listener({ type: 'vcc:preview-extract', tabId: 7 }, {})).resolves.toMatchObject({
      type: 'vcc:preview-result',
    });
    expect(scriptingExecute).toHaveBeenCalledWith({
      target: { tabId: 7 },
      files: ['content-scripts/listing.js'],
    });
    expect(tabsSend).toHaveBeenCalledWith(7, { type: 'vcc:extract-request' });
  });

  it('reports injection failures instead of a listing', async () => {
    tabsGet.mockResolvedValueOnce({ url: 'https://dealer.example/inventory/42' });
    tabsSend.mockRejectedValueOnce(new Error('no receiving end'));
    scriptingExecute.mockRejectedValueOnce(new Error('denied'));
    await expect(listener({ type: 'vcc:preview-extract', tabId: 7 }, {})).resolves.toMatchObject({
      type: 'vcc:error',
      message: expect.stringMatching(/scripting blocked/),
    });
  });

  it('refuses to inspect non-web pages', async () => {
    tabsGet.mockResolvedValueOnce({ url: 'chrome://extensions/' });
    // Privileged pages never host content scripts: the ask fails, and the
    // scheme check refuses injection before anything is injected.
    tabsSend.mockRejectedValueOnce(new Error('no receiving end'));
    await expect(listener({ type: 'vcc:preview-extract', tabId: 7 }, {})).resolves.toMatchObject({
      type: 'vcc:error',
      message: expect.stringMatching(/only web pages/),
    });
    expect(scriptingExecute).not.toHaveBeenCalled();
  });

  it('applies operator overrides and enforces the photo subset', async () => {
    const listing = {
      ...validListing(),
      photos: [
        { url: 'http://127.0.0.1:8901/photos/a.jpg' },
        { url: 'http://127.0.0.1:8901/photos/b.jpg' },
      ],
    };
    tabsSend.mockResolvedValueOnce({ type: 'vcc:extract-result', listing });
    runImportMock.mockResolvedValueOnce({ status: 'complete', listingId: 'L9' });
    await expect(
      listener(
        {
          type: 'vcc:start-import',
          tabId: 7,
          overrides: { year: 2021 },
          photoUrls: ['http://127.0.0.1:8901/photos/b.jpg', 'https://invented.example/evil.jpg'],
        },
        {},
      ),
    ).resolves.toMatchObject({ ok: true });
    const effective = runImportMock.mock.calls[0][1] as typeof listing;
    expect(effective.year).toBe(2021);
    // The invented URL is dropped: selection only, never typed evidence.
    expect(effective.photos).toEqual([{ url: 'http://127.0.0.1:8901/photos/b.jpg' }]);
  });

  it('imports a manual entry and uploads user files as user-supplied', async () => {
    tabsGet.mockResolvedValueOnce({ url: 'https://dealer.example/inventory/9' });
    runImportMock.mockResolvedValueOnce({ status: 'partial', listingId: 'L10' });
    await expect(
      listener(
        {
          type: 'vcc:start-import',
          tabId: 7,
          manualEntry: {
            title: '2018 Mazda 3',
            year: 2018,
            make: 'Mazda',
            model: '3',
            trim: '',
            description: '',
          },
          userFiles: [{ name: 'dash.jpg', dataUrl: 'data:image/jpeg;base64,AAAA' }],
        },
        {},
      ),
    ).resolves.toMatchObject({ ok: true });
    const effective = runImportMock.mock.calls[0][1] as { extractorId: string; photos: unknown[] };
    expect(effective.extractorId).toBe('manual-entry');
    expect(effective.photos).toEqual([]);
    expect(scriptingExecute).not.toHaveBeenCalled();
    expect(uploadPhotoMock).toHaveBeenCalledTimes(1);
    expect(uploadPhotoMock.mock.calls[0][4]).toBeNull();
    expect(uploadPhotoMock.mock.calls[0][6]).toEqual({ userSupplied: true });
  });

  it('surfaces user-file upload failures as a warning, not an import failure', async () => {
    runImportMock.mockResolvedValueOnce({ status: 'complete', listingId: 'L11' });
    uploadPhotoMock.mockRejectedValueOnce(new Error('too large'));
    await expect(
      listener(
        {
          type: 'vcc:start-import',
          tabId: 7,
          userFiles: [{ name: 'dash.jpg', dataUrl: 'data:image/jpeg;base64,AAAA' }],
        },
        {},
      ),
    ).resolves.toMatchObject({ ok: true, warning: expect.stringMatching(/dash\.jpg/) });
  });

  it('reports per-origin host access', async () => {
    permissionsContains.mockImplementation(async (query: unknown) => {
      const origins = (query as { origins: string[] }).origins;
      return !origins[0].includes('blocked');
    });
    await expect(
      listener(
        {
          type: 'vcc:check-hosts',
          origins: ['https://cdn.example.com', 'https://blocked.example'],
        },
        {},
      ),
    ).resolves.toEqual({
      type: 'vcc:host-access',
      access: { 'https://cdn.example.com': true, 'https://blocked.example': false },
    });
    expect(permissionsContains).toHaveBeenCalledWith({
      origins: ['https://cdn.example.com/*'],
    });
  });
});
