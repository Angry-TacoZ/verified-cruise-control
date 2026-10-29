import { describe, expect, it, vi } from 'vitest';
import type { ExtractedListing } from './protocol';
import { runImport, type ImportProgress, type ProgressStore } from './uploader';

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

/** Mock fetch routing: listing create, photo downloads, photo uploads. */
function mockBackend(
  hooks: {
    failDownloads?: Set<string>;
    failOnce?: Set<string>;
    uploads?: Map<string, number>;
  } = {},
) {
  const uploads = hooks.uploads ?? new Map<string, number>();
  const seenFailOnce = new Set<string>();
  return vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const href = String(typeof url === 'string' ? url : ((url as Request).url ?? url));
    if (href.endsWith('/api/extension/listings') && init?.method === 'POST') {
      return new Response(JSON.stringify({ id: 'L1', import_status: 'importing' }), {
        status: 201,
      });
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

describe('runImport', () => {
  it('imports a listing and uploads every photo once', async () => {
    const uploads = new Map<string, number>();
    const fetchMock = mockBackend({ uploads });
    const store = memoryStore();
    const progress = await runImport(
      'tok',
      listingWithPhotos(['https://cdn.example.test/a.jpg', 'https://cdn.example.test/b.jpg']),
      { fetchImpl: fetchMock as unknown as typeof fetch, store, concurrency: 2 },
    );
    expect(progress.finished).toBe(true);
    expect(progress.listingId).toBe('L1');
    expect(progress.photos.map((p) => p.status)).toEqual(['done', 'done']);
    expect(uploads.get('https://cdn.example.test/a.jpg')).toBe(1);
    expect(uploads.get('https://cdn.example.test/b.jpg')).toBe(1);
    expect(progress.error).toBeUndefined();
  });

  it('retries a flaky photo and still uploads once', async () => {
    const uploads = new Map<string, number>();
    const fetchMock = mockBackend({
      uploads,
      failOnce: new Set(['https://cdn.example.test/a.jpg']),
    });
    const progress = await runImport('tok', listingWithPhotos(['https://cdn.example.test/a.jpg']), {
      fetchImpl: fetchMock as unknown as typeof fetch,
      store: memoryStore(),
      maxAttempts: 2,
    });
    expect(progress.photos[0]?.status).toBe('done');
    expect(uploads.get('https://cdn.example.test/a.jpg')).toBe(1);
  });

  it('marks persistent failures without failing the whole run', async () => {
    const fetchMock = mockBackend({ failDownloads: new Set(['https://cdn.example.test/bad.jpg']) });
    const progress = await runImport(
      'tok',
      listingWithPhotos(['https://cdn.example.test/good.jpg', 'https://cdn.example.test/bad.jpg']),
      { fetchImpl: fetchMock as unknown as typeof fetch, store: memoryStore(), maxAttempts: 1 },
    );
    expect(progress.finished).toBe(true);
    expect(progress.photos.map((p) => p.status).sort()).toEqual(['done', 'failed']);
    expect(progress.error).toContain('1 of 2 photos failed');
  });

  it('resumes after interruption without re-uploading finished photos', async () => {
    const uploads = new Map<string, number>();
    // First run: second photo fails permanently (for now).
    const failing = mockBackend({
      uploads,
      failDownloads: new Set(['https://cdn.example.test/b.jpg']),
    });
    const store = memoryStore();
    const urls = ['https://cdn.example.test/a.jpg', 'https://cdn.example.test/b.jpg'];
    const first = await runImport('tok', listingWithPhotos(urls), {
      fetchImpl: failing as unknown as typeof fetch,
      store,
      maxAttempts: 1,
      concurrency: 1,
    });
    expect(first.finished).toBe(true);
    // Simulate worker restart: resume from the stored checkpoint with a
    // fresh store handle over the same data (b still pending).
    const checkpoint = await store.load();
    expect(checkpoint).not.toBeNull();
    checkpoint!.finished = false; // worker died before marking finished
    checkpoint!.photos[1]!.status = 'pending';
    const resumedStore = memoryStore(checkpoint);
    const working = mockBackend({ uploads });
    const second = await runImport('tok', listingWithPhotos(urls), {
      fetchImpl: working as unknown as typeof fetch,
      store: resumedStore,
      maxAttempts: 1,
      concurrency: 1,
    });
    expect(second.listingId).toBe('L1'); // same listing reused, not recreated
    expect(second.photos.map((p) => p.status)).toEqual(['done', 'done']);
    expect(uploads.get('https://cdn.example.test/a.jpg')).toBe(1); // not re-uploaded
    expect(uploads.get('https://cdn.example.test/b.jpg')).toBe(1);
  });

  it('honours cancellation between photos', async () => {
    const fetchMock = mockBackend();
    let calls = 0;
    const progress = await runImport(
      'tok',
      listingWithPhotos(['https://cdn.example.test/a.jpg', 'https://cdn.example.test/b.jpg']),
      {
        fetchImpl: fetchMock as unknown as typeof fetch,
        store: memoryStore(),
        concurrency: 1,
        isCancelled: () => {
          calls += 1;
          return calls > 1;
        },
      },
    );
    expect(progress.finished).toBe(true);
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
      fetchImpl: fetchMock as unknown as typeof fetch,
      store: memoryStore(),
    });
    expect(progress.photos[0]?.status).toBe('duplicate');
    expect(progress.error).toBeUndefined();
  });
});
