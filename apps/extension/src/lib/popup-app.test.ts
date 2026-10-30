import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../../entrypoints/popup/App.svelte';
import type { ImportProgress } from './uploader';

const { sendMessageMock, queryMock, storeState } = vi.hoisted(() => ({
  sendMessageMock: vi.fn(),
  queryMock: vi.fn(),
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

  it('surfaces retry failures without losing the partial state', async () => {
    render(App);
    const retry = await screen.findByRole('button', { name: /Retry 1 failed photo/ });
    sendMessageMock.mockResolvedValueOnce({ ok: false, error: 'backend down' });
    await fireEvent.click(retry);
    expect(await screen.findByText('backend down')).toBeInTheDocument();
    expect(await screen.findByText(/Import partially complete/)).toBeInTheDocument();
  });
});
