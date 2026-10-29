import { describe, expect, it, vi } from 'vitest';
import { ExtensionApiError } from './api';
import type { ExtractedListing } from './protocol';
import {
  classifyFailure,
  normalizeProgress,
  retryFailedPhotos,
  runImport,
  type FailureReport,
  type ImportDeps,
  type ImportProgress,
  type ProgressStore,
} from './uploader';

function memoryStore(seed: ImportProgress | null = null): ProgressStore {
  let current = seed;
  return {
    load: async () => current,
    save: async (p) => {
      current = structuredClone(p);
    },
    clear: async () => {
      current = null;
    },
  };
}

function listingWithPhotos(urls: string[]): ExtractedListing {
  return {
    sourceUrl: 'http://127.0.0.1:8901/l/1',
    sourceSite: 'example.test',
    title: 't',
    year: null,
    make: '',
    model: '',
    trim: '',
    vin: null,
    description: '',
    advertisedClaims: [],
    photos: urls.map((url) => ({ url })),
    extractorId: 'synthetic-v1',
  };
}

function photoBytes(label: string): Response {
  return new Response(`fake-bytes-${label}`, {
    headers: { 'content-type': 'image/jpeg', 'content-length': '32' },
  });
}

/** Mock fetch routing: listing create, photo downloads, photo uploads,
 * failure reports. */
function mockBackend(
  hooks: {
    failDownloads?: Set<string>;
    failOnce?: Set<string>;
    uploads?: Map<string, number>;
    reports?: FailureReport[];
    creates?: { count: number };
  } = {},
) {
  const uploads = hooks.uploads ?? new Map<string, number>();
  const creates = hooks.creates ?? { count: 0 };
  const seenFailOnce = new Set<string>();
  return vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const href = String(typeof url === 'string' ? url : ((url as Request).url ?? url));
    if (href.endsWith('/api/extension/listings') && init?.method === 'POST') {
      creates.count += 1;
      return new Response(JSON.stringify({ id: 'L1', import_status: 'importing' }), {
        status: 201,
      });
    }
    if (href.endsWith('/failed-photos') && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as {
        source_url: string | null;
        kind: FailureReport['kind'];
        detail: string;
      };
      hooks.reports?.push({ sourceUrl: body.source_url, kind: body.kind, detail: body.detail });
      return new Response(
        JSON.stringify({ id: 'F1', key: 'k', kind: body.kind, resolved: false }),
        { status: 201 },
      );
    }
    if (href.includes('/photos') && init?.method === 'POST') {
      const form = init.body as FormData;
      const source = String(form.get('source_url') ?? '');
      uploads.set(source, (uploads.get(source) ?? 0) + 1);
      return new Response(
        JSON.stringify({ id: `P-${uploads.size}`, sha256: 'x', duplicate: false }),
      );
    }
    if (hooks.failDownloads?.has(href)) throw new Error('network down');
    if (hooks.failOnce?.has(href) && !seenFailOnce.has(href)) {
      seenFailOnce.add(href);
      throw new Error('flaky network');
    }
    return photoBytes(href);
  });
}

function depsFor(
  store: ProgressStore,
  fetchMock: ReturnType<typeof mockBackend>,
  extra: Partial<ImportDeps> = {},
  reports?: FailureReport[],
): ImportDeps {
  return {
    fetchImpl: fetchMock as unknown as typeof fetch,
    store,
    concurrency: 2,
    maxAttempts: 2,
    reportFailure:
      reports === undefined
        ? async () => {}
        : async (report) => {
            reports.push(report);
          },
    ...extra,
  };
}

describe('runImport', () => {
  it('imports a listing and uploads every photo once', async () => {
    const uploads = new Map<string, number>();
    const reports: FailureReport[] = [];
    const creates = { count: 0 };
    const fetchMock = mockBackend({ uploads, reports, creates });
    const progress = await runImport(
      'tok',
      listingWithPhotos(['https://cdn.example.test/a.jpg', 'https://cdn.example.test/b.jpg']),
      depsFor(memoryStore(), fetchMock),
    );
    expect(progress.status).toBe('complete');
    expect(progress.listingId).toBe('L1');
    expect(progress.photos.map((p) => p.status)).toEqual(['done', 'done']);
    expect(uploads.get('https://cdn.example.test/a.jpg')).toBe(1);
    expect(uploads.get('https://cdn.example.test/b.jpg')).toBe(1);
    expect(progress.error).toBeUndefined();
    expect(reports).toHaveLength(0);
    expect(creates.count).toBe(1);
  });

  it('retries a flaky photo and still uploads once', async () => {
    const uploads = new Map<string, number>();
    const fetchMock = mockBackend({
      uploads,
      creates: { count: 0 },
      failOnce: new Set(['https://cdn.example.test/a.jpg']),
    });
    const progress = await runImport('tok', listingWithPhotos(['https://cdn.example.test/a.jpg']), {
      ...depsFor(memoryStore(), fetchMock),
      maxAttempts: 2,
    });
    expect(progress.photos[0]?.status).toBe('done');
    expect(progress.status).toBe('complete');
    expect(uploads.get('https://cdn.example.test/a.jpg')).toBe(1);
  });

  it('marks persistent failures partial and reports them to the backend', async () => {
    const reports: FailureReport[] = [];
    const fetchMock = mockBackend({
      reports,
      creates: { count: 0 },
      failDownloads: new Set(['https://cdn.example.test/bad.jpg']),
    });
    const progress = await runImport(
      'tok',
      listingWithPhotos(['https://cdn.example.test/good.jpg', 'https://cdn.example.test/bad.jpg']),
      depsFor(memoryStore(), fetchMock, { maxAttempts: 1 }, reports),
    );
    expect(progress.status).toBe('partial');
    expect(progress.photos.map((p) => p.status).sort()).toEqual(['done', 'failed']);
    expect(progress.error).toContain('1 of 2 photos failed');
    expect(reports).toHaveLength(1);
    expect(reports[0]).toMatchObject({
      sourceUrl: 'https://cdn.example.test/bad.jpg',
      kind: 'download',
    });
  });

  it('marks an all-failed run failed', async () => {
    const fetchMock = mockBackend({
      creates: { count: 0 },
      failDownloads: new Set(['https://cdn.example.test/bad.jpg']),
    });
    const progress = await runImport(
      'tok',
      listingWithPhotos(['https://cdn.example.test/bad.jpg']),
      {
        ...depsFor(memoryStore(), fetchMock),
        maxAttempts: 1,
      },
    );
    expect(progress.status).toBe('failed');
    expect(progress.error).toContain('all 1 photos failed');
  });

  it('resumes an interrupted active run without recreating the listing', async () => {
    const uploads = new Map<string, number>();
    const creates = { count: 0 };
    const seed: ImportProgress = {
      runId: 'r1',
      listingId: 'L1',
      sourceUrl: 'http://127.0.0.1:8901/l/1',
      photos: [
        { url: 'https://cdn.example.test/a.jpg', status: 'done' },
        { url: 'https://cdn.example.test/b.jpg', status: 'pending' },
      ],
      startedAt: 1,
      updatedAt: 1,
      status: 'active',
    };
    const fetchMock = mockBackend({ uploads, creates });
    const progress = await runImport(
      'tok',
      listingWithPhotos(['https://cdn.example.test/a.jpg', 'https://cdn.example.test/b.jpg']),
      depsFor(memoryStore(seed), fetchMock),
    );
    expect(progress.listingId).toBe('L1');
    expect(progress.photos.map((p) => p.status)).toEqual(['done', 'done']);
    expect(creates.count).toBe(0); // resumed, not recreated
    expect(uploads.get('https://cdn.example.test/a.jpg')).toBeUndefined();
    expect(uploads.get('https://cdn.example.test/b.jpg')).toBe(1);
  });

  it('does not resume a finished partial run — recovery uses retryFailedPhotos', async () => {
    const creates = { count: 0 };
    const seed: ImportProgress = {
      runId: 'r1',
      listingId: 'L1',
      sourceUrl: 'http://127.0.0.1:8901/l/1',
      photos: [{ url: 'https://cdn.example.test/b.jpg', status: 'failed', detail: 'x' }],
      startedAt: 1,
      updatedAt: 1,
      status: 'partial',
      error: '1 of 1 photos failed',
    };
    const store = memoryStore(seed);
    const failing = mockBackend({
      creates,
      failDownloads: new Set(['https://cdn.example.test/b.jpg']),
    });
    const progress = await runImport('tok', listingWithPhotos(['https://cdn.example.test/b.jpg']), {
      ...depsFor(store, failing),
      maxAttempts: 1,
    });
    expect(creates.count).toBe(1); // fresh listing: new user intent, not a resume
    expect(progress.listingId).toBe('L1');
    expect(progress).not.toBe(seed);
  });

  it('honours cancellation between photos', async () => {
    const fetchMock = mockBackend({ creates: { count: 0 } });
    let calls = 0;
    const progress = await runImport(
      'tok',
      listingWithPhotos(['https://cdn.example.test/a.jpg', 'https://cdn.example.test/b.jpg']),
      {
        ...depsFor(memoryStore(), fetchMock),
        concurrency: 1,
        isCancelled: () => {
          calls += 1;
          return calls > 1;
        },
      },
    );
    // First photo stored, second cancelled: partial, with the cause recorded.
    expect(progress.status).toBe('partial');
    expect(progress.photos.some((p) => p.detail === 'cancelled')).toBe(true);
  });

  it('skips already-duplicate photos reported by the server', async () => {
    const fetchMock = vi.fn(async (url: string | URL | Request) => {
      const href = String(url);
      if (href.endsWith('/api/extension/listings')) {
        return new Response(JSON.stringify({ id: 'L1' }), { status: 201 });
      }
      if (href.includes('/photos')) {
        return new Response(JSON.stringify({ id: 'P1', sha256: 'x', duplicate: true }));
      }
      return photoBytes(href);
    });
    const progress = await runImport('tok', listingWithPhotos(['https://cdn.example.test/a.jpg']), {
      ...depsFor(memoryStore(), fetchMock),
    });
    expect(progress.photos[0]?.status).toBe('duplicate');
    expect(progress.status).toBe('complete');
    expect(progress.error).toBeUndefined();
  });

  it('survives a failing failure-reporter', async () => {
    const fetchMock = mockBackend({
      creates: { count: 0 },
      failDownloads: new Set(['https://cdn.example.test/bad.jpg']),
    });
    const progress = await runImport(
      'tok',
      listingWithPhotos(['https://cdn.example.test/bad.jpg']),
      {
        ...depsFor(memoryStore(), fetchMock, {
          reportFailure: async () => {
            throw new Error('backend down');
          },
        }),
        maxAttempts: 1,
      },
    );
    expect(progress.status).toBe('failed');
  });
});

describe('retryFailedPhotos', () => {
  const urls = [
    'https://cdn.example.test/a.jpg',
    'https://cdn.example.test/b.jpg',
    'https://cdn.example.test/c.jpg',
  ];

  async function partialRun() {
    const uploads = new Map<string, number>();
    const reports: FailureReport[] = [];
    const creates = { count: 0 };
    const failing = mockBackend({
      uploads,
      reports,
      creates,
      failDownloads: new Set(['https://cdn.example.test/b.jpg']),
    });
    const store = memoryStore();
    const first = await runImport('tok', listingWithPhotos(urls), {
      ...depsFor(store, failing),
      maxAttempts: 1,
      concurrency: 1,
    });
    expect(first.status).toBe('partial');
    return { store, uploads, reports, creates };
  }

  it('retries one failed photo, reuses the listing, stores all three', async () => {
    const { store, uploads, creates } = await partialRun();
    const working = mockBackend({ uploads, creates });
    const retried = await retryFailedPhotos('tok', {
      ...depsFor(store, working),
      maxAttempts: 1,
      concurrency: 1,
    });
    expect(retried.listingId).toBe('L1');
    expect(creates.count).toBe(1); // no new listing created
    expect(retried.photos.map((p) => p.status)).toEqual(['done', 'done', 'done']);
    expect(retried.status).toBe('complete');
    // Successful photos were not uploaded again; the failed one exactly once.
    expect(uploads.get('https://cdn.example.test/a.jpg')).toBe(1);
    expect(uploads.get('https://cdn.example.test/c.jpg')).toBe(1);
    expect(uploads.get('https://cdn.example.test/b.jpg')).toBe(1);
  });

  it('is a no-op when nothing failed', async () => {
    const uploads = new Map<string, number>();
    const creates = { count: 0 };
    const fetchMock = mockBackend({ uploads, creates });
    const store = memoryStore();
    const done = await runImport('tok', listingWithPhotos([urls[0]]), depsFor(store, fetchMock));
    expect(done.status).toBe('complete');
    const again = await retryFailedPhotos('tok', depsFor(store, fetchMock));
    expect(again.status).toBe('complete');
    expect(creates.count).toBe(1);
    expect(uploads.get(urls[0])).toBe(1);
  });

  it('refuses to retry while a run is active', async () => {
    const store = memoryStore({
      runId: 'r1',
      listingId: 'L1',
      sourceUrl: 'http://127.0.0.1:8901/l/1',
      photos: [{ url: urls[0], status: 'pending' }],
      startedAt: 1,
      updatedAt: 1,
      status: 'active',
    });
    await expect(retryFailedPhotos('tok', depsFor(store, mockBackend()))).rejects.toThrow(
      /still running/,
    );
  });

  it('throws when there is nothing to retry', async () => {
    await expect(retryFailedPhotos('tok', depsFor(memoryStore(), mockBackend()))).rejects.toThrow(
      /no import to retry/,
    );
  });

  it('keeps partial status when the retry also fails', async () => {
    const { store, creates } = await partialRun();
    const stillFailing = mockBackend({
      creates,
      failDownloads: new Set(['https://cdn.example.test/b.jpg']),
    });
    const retried = await retryFailedPhotos('tok', {
      ...depsFor(store, stillFailing),
      maxAttempts: 1,
      concurrency: 1,
    });
    expect(retried.listingId).toBe('L1');
    expect(creates.count).toBe(1);
    expect(retried.status).toBe('partial');
  });
});

describe('classifyFailure', () => {
  it('maps auth failures to permission', () => {
    expect(classifyFailure(new ExtensionApiError(403, 'x'), 'download')).toBe('permission');
    expect(classifyFailure(new ExtensionApiError(401, 'x'), 'upload')).toBe('permission');
  });

  it('maps upload-phase 422 to validation', () => {
    expect(classifyFailure(new ExtensionApiError(422, 'x'), 'upload')).toBe('validation');
  });

  it('maps capacity and generic errors to the dying phase', () => {
    expect(classifyFailure(new ExtensionApiError(413, 'x'), 'upload')).toBe('upload');
    expect(classifyFailure(new Error('network down'), 'download')).toBe('download');
    expect(classifyFailure(new Error('boom'), 'upload')).toBe('upload');
  });
});

describe('normalizeProgress', () => {
  const base = {
    runId: 'r1',
    listingId: 'L1',
    sourceUrl: 'http://127.0.0.1:8901/l/1',
    startedAt: 1,
    updatedAt: 1,
  };

  it('passes current shapes through', () => {
    const current: ImportProgress = {
      ...base,
      photos: [{ url: 'a', status: 'done' }],
      status: 'partial',
    };
    expect(normalizeProgress(current)).toBe(current);
  });

  it('maps the legacy finished flag to a status', () => {
    expect(
      normalizeProgress({ ...base, photos: [{ url: 'a', status: 'done' }], finished: true })
        ?.status,
    ).toBe('complete');
    expect(
      normalizeProgress({ ...base, photos: [{ url: 'a', status: 'failed' }], finished: true })
        ?.status,
    ).toBe('failed');
    expect(
      normalizeProgress({
        ...base,
        photos: [
          { url: 'a', status: 'done' },
          { url: 'b', status: 'failed' },
        ],
        finished: true,
      })?.status,
    ).toBe('partial');
    expect(
      normalizeProgress({ ...base, photos: [{ url: 'a', status: 'pending' }], finished: false })
        ?.status,
    ).toBe('active');
  });

  it('rejects garbage', () => {
    expect(normalizeProgress(null)).toBeNull();
    expect(normalizeProgress({})).toBeNull();
    expect(normalizeProgress({ sourceUrl: 'x', photos: [] })).toBeNull();
  });
});
