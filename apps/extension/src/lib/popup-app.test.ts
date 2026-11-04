import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../../entrypoints/popup/App.svelte';
import type { ImportProgress } from './uploader';

const { sendMessageMock, queryMock, requestPermissionsMock, storeState } = vi.hoisted(() => ({
  sendMessageMock: vi.fn(),
  queryMock: vi.fn(),
  requestPermissionsMock: vi.fn(),
  storeState: {
    token: 'tok' as string | null,
    progress: null as ImportProgress | null,
    tokenFailure: null as string | null,
  },
}));

vi.mock('wxt/browser', () => ({
  browser: {
    tabs: { query: (...args: unknown[]) => queryMock(...args) },
    runtime: { sendMessage: (...args: unknown[]) => sendMessageMock(...args) },
    permissions: { request: (...args: unknown[]) => requestPermissionsMock(...args) },
    storage: { local: {}, session: {} },
  },
}));

vi.mock('./store', () => ({
  getPairingToken: async () => {
    if (storeState.tokenFailure) throw new Error(storeState.tokenFailure);
    return storeState.token;
  },
  setPairingToken: async (token: string | null) => {
    if (storeState.tokenFailure) throw new Error(storeState.tokenFailure);
    storeState.token = token;
  },
  sessionProgressStore: () => ({
    load: async () => storeState.progress,
    save: async (progress: ImportProgress) => {
      storeState.progress = progress;
    },
    clear: async () => {
      storeState.progress = null;
    },
  }),
  setImportCancelled: async () => {},
  isImportCancelled: async () => false,
  ensureTrustedStorage: async () => {},
}));

afterEach(() => cleanup());

function partialProgress(): ImportProgress {
  return {
    runId: 'r1',
    listingId: 'L1',
    sourceUrl: 'http://127.0.0.1:8901/l/1',
    photos: [
      { url: 'http://127.0.0.1:8901/photos/front.jpg', status: 'done' },
      { url: 'http://127.0.0.1:8901/photos/missing.jpg', status: 'failed', detail: '404' },
    ],
    startedAt: 1,
    updatedAt: 2,
    status: 'partial',
    error: '1 of 2 photos failed',
  };
}

describe('popup retry UI', () => {
  beforeEach(() => {
    storeState.token = 'tok';
    storeState.tokenFailure = null;
    storeState.progress = partialProgress();
    queryMock.mockResolvedValue([{ id: 7, url: 'http://127.0.0.1:8901/l/1' }]);
    sendMessageMock.mockResolvedValue({ ok: true, progress: storeState.progress });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('ok')),
    );
  });

  it('offers a retry that reuses the stored listing, then shows completion', async () => {
    render(App);
    expect(await screen.findByText(/Import partially complete/)).toBeInTheDocument();
    const retry = await screen.findByRole('button', { name: /Retry 1 failed photo/ });
    const completed: ImportProgress = {
      ...partialProgress(),
      photos: partialProgress().photos.map((p) => ({ ...p, status: 'done' as const })),
      status: 'complete',
      error: undefined,
    };
    sendMessageMock.mockResolvedValueOnce({ ok: true, progress: completed });
    await fireEvent.click(retry);
    expect(sendMessageMock).toHaveBeenCalledWith({ type: 'vcc:retry-failed' });
    expect(await screen.findByText(/Import complete: 2\/2 photos/)).toBeInTheDocument();
  });

  it('shows a storage-restriction error instead of pairing when trusted storage fails', async () => {
    storeState.tokenFailure = 'pairing-token storage is unavailable: no setAccessLevel';
    storeState.progress = null;
    render(App);
    expect(await screen.findByRole('alert')).toHaveTextContent(/storage is unavailable/);
    expect(screen.queryByLabelText(/Pairing token/)).not.toBeInTheDocument();
  });

  it('offers resume or abandon for an interrupted import with no running operation', async () => {
    storeState.progress = {
      runId: 'r1',
      listingId: 'L1',
      sourceUrl: 'http://127.0.0.1:8901/l/1',
      photos: [
        { url: 'http://127.0.0.1:8901/photos/front.jpg', status: 'done' },
        { url: 'http://127.0.0.1:8901/photos/missing.jpg', status: 'pending' },
      ],
      startedAt: 1,
      updatedAt: 2,
      status: 'active',
    };
    const failed = {
      ...storeState.progress,
      photos: (storeState.progress as ImportProgress).photos.map((p) =>
        p.status === 'done' ? p : { ...p, status: 'failed' as const },
      ),
      status: 'failed' as const,
      error: 'import abandoned by operator before completion',
    };
    sendMessageMock.mockImplementation(async (message: unknown) => {
      const type = (message as { type?: string }).type;
      if (type === 'vcc:get-state') return { operationRunning: false };
      if (type === 'vcc:abandon-import') return { ok: true, progress: failed };
      return { ok: true, progress: storeState.progress };
    });
    render(App);
    expect(await screen.findByText(/no upload is running/)).toBeInTheDocument();
    expect(screen.queryByText(/Uploading photos/)).not.toBeInTheDocument();
    const abandon = await screen.findByRole('button', { name: 'Abandon import' });
    expect(await screen.findByRole('button', { name: 'Resume import' })).toBeInTheDocument();
    await fireEvent.click(abandon);
    expect(sendMessageMock).toHaveBeenCalledWith({ type: 'vcc:abandon-import' });
    expect(await screen.findByText(/Import failed/)).toBeInTheDocument();
  });

  it('surfaces cancellation failures instead of silently refreshing', async () => {
    storeState.progress = {
      runId: 'r1',
      listingId: 'L1',
      sourceUrl: 'http://127.0.0.1:8901/l/1',
      photos: [
        { url: 'http://127.0.0.1:8901/photos/front.jpg', status: 'done' },
        { url: 'http://127.0.0.1:8901/photos/rear.jpg', status: 'uploading' },
      ],
      startedAt: 1,
      updatedAt: 2,
      status: 'active',
    };
    sendMessageMock.mockImplementation(async (message: unknown) => {
      const type = (message as { type?: string }).type;
      if (type === 'vcc:get-state') return { operationRunning: true };
      if (type === 'vcc:cancel-import') return { ok: false, error: 'db locked' };
      return { ok: true, progress: storeState.progress };
    });
    render(App);
    const cancel = await screen.findByRole('button', { name: 'Cancel' });
    await fireEvent.click(cancel);
    expect(sendMessageMock).toHaveBeenCalledWith({ type: 'vcc:cancel-import' });
    expect(await screen.findByText('db locked')).toBeInTheDocument();
  });

  it('surfaces retry failures without losing the partial state', async () => {
    render(App);
    const retry = await screen.findByRole('button', { name: /Retry 1 failed photo/ });
    sendMessageMock.mockResolvedValueOnce({ ok: false, error: 'backend down' });
    await fireEvent.click(retry);
    expect(await screen.findByText('backend down')).toBeInTheDocument();
    expect(await screen.findByText(/Import partially complete/)).toBeInTheDocument();
  });
});

describe('popup operator review', () => {
  const detected = {
    sourceUrl: 'https://dealer.example/inventory/42',
    sourceSite: 'dealer.example',
    title: '2021 Toyota Camry SE',
    year: 2021,
    make: 'Toyota',
    model: 'Camry',
    trim: 'SE',
    vin: null,
    description: 'A used sedan.',
    advertisedClaims: ['adaptive cruise control'],
    photos: [{ url: 'https://cdn.example.com/a.jpg' }, { url: 'https://cdn.example.com/b.jpg' }],
    extractorId: 'generic-v1',
    extraction: { confidence: 'medium', reasons: ['year and make identified'], fieldSources: {} },
  };

  beforeEach(() => {
    storeState.token = 'tok';
    storeState.tokenFailure = null;
    storeState.progress = null;
    queryMock.mockResolvedValue([{ id: 7, url: 'https://dealer.example/inventory/42' }]);
    requestPermissionsMock.mockResolvedValue(true);
    sendMessageMock.mockImplementation(async (message: unknown) => {
      const type = (message as { type?: string }).type;
      if (type === 'vcc:get-state') return { operationRunning: false };
      if (type === 'vcc:preview-extract') return { type: 'vcc:preview-result', listing: detected };
      if (type === 'vcc:check-hosts')
        return {
          type: 'vcc:host-access',
          access: { 'https://cdn.example.com': false },
        };
      return { ok: true, progress: null };
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('ok')),
    );
  });

  async function openReview() {
    render(App);
    // Wait for the tab query to settle: the Review button stays disabled
    // until tabReady, so clicking earlier would hit a null tab id.
    await screen.findByText(/Page to inspect:/);
    await fireEvent.click(await screen.findByRole('button', { name: 'Review listing' }));
    // The detected title lands in the editable title field.
    await screen.findByDisplayValue('2021 Toyota Camry SE');
    // Host access resolves after the preview renders (one grant button
    // per photo on the unpermitted origin).
    await screen.findAllByRole('button', { name: 'Allow host' });
  }

  it('previews the detected vehicle with editable fields and photo selection', async () => {
    await openReview();
    expect(screen.getByText(/medium confidence · generic-v1/)).toBeInTheDocument();
    expect(screen.getByText(/Seller claims \(1\)/)).toBeInTheDocument();
    // All photos selected by default.
    expect(screen.getByText(/Photos \(2\/2 selected\)/)).toBeInTheDocument();
  });

  it('requests host access behind an explicit operator action', async () => {
    await openReview();
    const grants = screen.getAllByRole('button', { name: 'Allow host' });
    expect(grants).toHaveLength(2);
    await fireEvent.click(grants[0]);
    expect(requestPermissionsMock).toHaveBeenCalledWith({
      origins: ['https://cdn.example.com/*'],
    });
  });

  it('sends only changed fields as overrides and the photo subset', async () => {
    await openReview();
    // Deselect the second photo.
    const boxes = screen.getAllByRole('checkbox');
    await fireEvent.click(boxes[1]);
    // Correct the trim.
    const trim = screen.getByLabelText('Trim') as HTMLInputElement;
    await fireEvent.input(trim, { target: { value: 'XSE' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Verify Adaptive Cruise Control' }));
    expect(sendMessageMock).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'vcc:start-import',
        tabId: 7,
        requestedCheck: 'adaptive_cruise_control',
        overrides: { trim: 'XSE' },
        photoUrls: ['https://cdn.example.com/a.jpg'],
      }),
    );
  });

  it('falls back to manual entry with user-supplied photos', async () => {
    sendMessageMock.mockImplementation(async (message: unknown) => {
      const type = (message as { type?: string }).type;
      if (type === 'vcc:get-state') return { operationRunning: false };
      if (type === 'vcc:preview-extract')
        return { type: 'vcc:error', message: 'no listing detected on this page' };
      return { ok: true, progress: null };
    });
    render(App);
    await screen.findByText(/Page to inspect:/);
    await fireEvent.click(await screen.findByRole('button', { name: 'Review listing' }));
    expect(await screen.findByText(/no listing detected/)).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Enter manually' }));
    await fireEvent.input(screen.getByLabelText('Title'), {
      target: { value: '2018 Mazda 3' },
    });
    // Attach a local photo: labelled user-supplied, never page evidence.
    const png = new File([new Uint8Array([137, 80, 78, 71])], 'dash.png', { type: 'image/png' });
    const picker = screen.getByLabelText('Attach your own photos') as HTMLInputElement;
    await fireEvent.change(picker, { target: { files: [png] } });
    await screen.findByText('dash.png');
    await fireEvent.click(screen.getByRole('button', { name: 'Verify Adaptive Cruise Control' }));
    const sent = sendMessageMock.mock.calls.find(
      (call) => (call[0] as { type?: string }).type === 'vcc:start-import',
    )?.[0] as {
      manualEntry?: { title?: string };
      userFiles?: { name?: string; dataUrl?: string }[];
      photoUrls?: string[];
    };
    expect(sent.manualEntry?.title).toBe('2018 Mazda 3');
    expect(sent.photoUrls).toBeUndefined();
    expect(sent.userFiles).toHaveLength(1);
    expect(sent.userFiles?.[0].name).toBe('dash.png');
    expect(sent.userFiles?.[0].dataUrl).toMatch(/^data:image\/png;base64,/);
  });
});
