/** Import orchestration: listing create + bounded-concurrency photo uploads.
 *
 * Pure orchestration over injected dependencies (fetch, progress store) so
 * unit tests run without a browser. Progress is checkpointed after every
 * photo, so a suspended service worker resumes instead of restarting.
 */

import {
  MAX_DOWNLOAD_BYTES,
  createListing,
  downloadImageBytes,
  uploadPhoto,
  type ListingPayload,
} from './api';
import type { ExtractedListing } from './protocol';

export interface PhotoProgress {
  url: string;
  status: 'pending' | 'downloading' | 'uploading' | 'done' | 'duplicate' | 'failed';
  detail?: string;
}

export interface ImportProgress {
  runId: string;
  listingId: string | null;
  sourceUrl: string;
  photos: PhotoProgress[];
  startedAt: number;
  updatedAt: number;
  finished: boolean;
  error?: string;
}

export interface ProgressStore {
  load(): Promise<ImportProgress | null>;
  save(progress: ImportProgress): Promise<void>;
  clear(): Promise<void>;
}

export interface ImportDeps {
  fetchImpl: typeof fetch;
  store: ProgressStore;
  /** Max simultaneous photo pipelines. */
  concurrency?: number;
  /** Whole-image retries per photo (server dedupes by hash). */
  maxAttempts?: number;
  onProgress?: (progress: ImportProgress) => void;
  /** Cooperative cancellation, checked between photos. */
  isCancelled?: () => boolean;
}

export function toListingPayload(listing: ExtractedListing): ListingPayload {
  return {
    source_url: listing.sourceUrl,
    source_site: listing.sourceSite,
    year: listing.year,
    make: listing.make,
    model: listing.model,
    trim: listing.trim,
    vin: listing.vin,
    title: listing.title,
    description: listing.description,
    advertised_claims: listing.advertisedClaims,
    expected_photo_count: listing.photos.length,
  };
}

function summarize(progress: ImportProgress): void {
  progress.updatedAt = Date.now();
}

async function runPhoto(
  token: string,
  listingId: string,
  photo: PhotoProgress,
  deps: Required<Pick<ImportDeps, 'fetchImpl' | 'maxAttempts'>> & Pick<ImportDeps, 'isCancelled'>,
  checkpoint: () => Promise<void>,
): Promise<void> {
  if (photo.status === 'done' || photo.status === 'duplicate') return;
  let attempt = 0;
  for (;;) {
    attempt += 1;
    if (deps.isCancelled?.()) {
      photo.status = 'failed';
      photo.detail = 'cancelled';
      await checkpoint();
      return;
    }
    try {
      photo.status = 'downloading';
      await checkpoint();
      const { bytes, filename } = await downloadImageBytes(photo.url, deps.fetchImpl);
      if (bytes.size > MAX_DOWNLOAD_BYTES) throw new Error('image exceeds download size limit');
      photo.status = 'uploading';
      await checkpoint();
      const uploaded = await uploadPhoto(
        token,
        listingId,
        bytes,
        filename.slice(0, 120) || 'photo',
        photo.url,
        deps.fetchImpl,
      );
      photo.status = uploaded.duplicate ? 'duplicate' : 'done';
      photo.detail = undefined;
      await checkpoint();
      return;
    } catch (err) {
      const message = err instanceof Error ? err.message : 'upload failed';
      if (attempt >= deps.maxAttempts) {
        photo.status = 'failed';
        photo.detail = message;
        await checkpoint();
        return;
      }
      // Whole-image retry; the server dedupes by SHA-256, so a retry after a
      // half-finished attempt cannot duplicate evidence.
    }
  }
}

async function runPool<T>(items: T[], concurrency: number, worker: (item: T) => Promise<void>) {
  const queue = [...items];
  const lanes = Array.from({ length: Math.max(1, concurrency) }, async () => {
    while (queue.length > 0) {
      const item = queue.shift();
      if (item === undefined) return;
      await worker(item);
    }
  });
  await Promise.all(lanes);
}

/** Start (or resume) an import run. Resuming reuses the stored listing id and
 * skips photos already done/duplicate, so retries never duplicate evidence.
 */
export async function runImport(
  token: string,
  listing: ExtractedListing,
  deps: ImportDeps,
): Promise<ImportProgress> {
  const fetchImpl = deps.fetchImpl;
  const maxAttempts = deps.maxAttempts ?? 2;
  const concurrency = deps.concurrency ?? 3;

  const stored = await deps.store.load();
  let active: ImportProgress;
  if (stored !== null && !stored.finished && stored.sourceUrl === listing.sourceUrl) {
    active = stored;
  } else {
    const created = await createListing(token, toListingPayload(listing), fetchImpl);
    active = {
      runId: `${Date.now()}`,
      listingId: created.id,
      sourceUrl: listing.sourceUrl,
      photos: listing.photos.map((p) => ({ url: p.url, status: 'pending' as const })),
      startedAt: Date.now(),
      updatedAt: Date.now(),
      finished: false,
    };
  }
  const checkpoint = async () => {
    summarize(active);
    await deps.store.save(active);
    deps.onProgress?.(active);
  };
  await checkpoint();

  if (!active.listingId) throw new Error('import run has no listing id');
  const listingId = active.listingId;
  await runPool(active.photos, concurrency, (photo) =>
    runPhoto(
      token,
      listingId,
      photo,
      { fetchImpl, maxAttempts, isCancelled: deps.isCancelled },
      checkpoint,
    ),
  );

  active.finished = true;
  const failures = active.photos.filter((p) => p.status === 'failed');
  if (failures.length > 0) {
    active.error = `${failures.length} of ${active.photos.length} photos failed`;
  }
  await checkpoint();
  return active;
}
