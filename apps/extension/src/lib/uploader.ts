/** Import orchestration: listing create + bounded-concurrency photo uploads.
 *
 * Pure orchestration over injected dependencies (fetch, progress store,
 * failure reporting) so unit tests run without a browser. Progress is
 * checkpointed after every photo, so a suspended service worker resumes
 * instead of restarting.
 *
 * Run states distinguish active, partially completed, failed, and
 * successfully completed imports. A finished-with-failures run never
 * auto-retries into a new listing: recovery goes through the explicit
 * `retryFailedPhotos` operation, which reuses the original listing id and
 * skips photos already stored.
 */

import {
  ExtensionApiError,
  MAX_DOWNLOAD_BYTES,
  createListing,
  downloadImageBytes,
  sha256HexBytes,
  uploadPhoto,
  type ListingPayload,
} from './api';
import type { ExtractedListing, ListingOverrides, ManualEntry, RequestedCheck } from './protocol';

export interface PhotoProgress {
  url: string;
  status: 'pending' | 'downloading' | 'uploading' | 'done' | 'duplicate' | 'failed';
  detail?: string;
}

/** Import run lifecycle: active runs accept checkpoints; partial/failed runs
 * finish with failures outstanding; complete runs stored everything. */
export type ImportRunStatus = 'active' | 'partial' | 'complete' | 'failed';

/** Where a photo failed, matching the backend failure-kind vocabulary. */
export type FailureKind = 'download' | 'permission' | 'validation' | 'upload';

export interface ImportProgress {
  runId: string;
  listingId: string | null;
  sourceUrl: string;
  photos: PhotoProgress[];
  startedAt: number;
  updatedAt: number;
  status: ImportRunStatus;
  error?: string;
}

export interface FailureReport {
  sourceUrl: string | null;
  /** SHA-256 of the rejected bytes when the reporter holds them, so the
   * backend merges with the validation record it already stored for the
   * same upload instead of opening a second row. */
  key?: string;
  kind: FailureKind;
  detail: string;
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
  /** Whole-image retries per photo (the server dedupes by hash). */
  maxAttempts?: number;
  onProgress?: (progress: ImportProgress) => void;
  /** Cooperative cancellation, checked between photos. */
  isCancelled?: () => boolean;
  /** Best-effort backend failure report (never fails the run). The run's
   * own listing id is passed explicitly — never re-read from the shared
   * store at report time, so an overlapping run cannot misattribute
   * failures to the wrong listing. */
  reportFailure?: (listingId: string, report: FailureReport) => Promise<void>;
}

const MIN_MODEL_YEAR = 1900;
const MAX_MODEL_YEAR = 2100;

function checkedYear(year: number | null | undefined, field: string): number | null {
  if (year === undefined || year === null) return year ?? null;
  if (!Number.isInteger(year) || year < MIN_MODEL_YEAR || year > MAX_MODEL_YEAR) {
    throw new Error(`${field} year ${year} is out of range`);
  }
  return year;
}

/** Apply operator corrections to an extracted listing. Only whitelisted
 * scalar fields change; claims and photos are selected, never typed, so an
 * override can never fabricate seller equipment or photo evidence. Override
 * fields are marked `operator` provenance; untouched fields keep theirs. */
export function applyListingOverrides(
  listing: ExtractedListing,
  overrides: ListingOverrides,
): ExtractedListing {
  const next: ExtractedListing = {
    ...listing,
    title: overrides.title?.trim() ? overrides.title.trim().slice(0, 500) : listing.title,
    year: overrides.year === undefined ? listing.year : checkedYear(overrides.year, 'override'),
    make: overrides.make === undefined ? listing.make : overrides.make.trim().slice(0, 100),
    model: overrides.model === undefined ? listing.model : overrides.model.trim().slice(0, 100),
    trim: overrides.trim === undefined ? listing.trim : overrides.trim.trim().slice(0, 100),
    description:
      overrides.description === undefined
        ? listing.description
        : overrides.description.trim().slice(0, 20000),
  };
  const sources = { ...(listing.extraction?.fieldSources ?? {}) };
  for (const [field, value] of Object.entries(overrides)) {
    // A supplied override (including clearing a field to ''/null) is an
    // operator assertion; absent keys keep their detected provenance.
    if (value !== undefined && value !== '') sources[field] = 'operator';
  }
  if (listing.extraction) next.extraction = { ...listing.extraction, fieldSources: sources };
  return next;
}

/** Build a listing from operator manual entry for pages where no vehicle
 * was detected. Identity fields the operator leaves blank stay unknown;
 * claims are empty and photos arrive separately as user-supplied uploads. */
export function listingFromManualEntry(entry: ManualEntry, pageUrl: string): ExtractedListing {
  let hostname: string;
  try {
    hostname = new URL(pageUrl).hostname.toLowerCase();
  } catch {
    hostname = '';
  }
  return {
    sourceUrl: pageUrl,
    sourceSite: hostname,
    title: entry.title.trim().slice(0, 500),
    year: checkedYear(entry.year, 'manual entry'),
    make: entry.make.trim().slice(0, 100),
    model: entry.model.trim().slice(0, 100),
    trim: entry.trim.trim().slice(0, 100),
    vin: null,
    description: entry.description.trim().slice(0, 20000),
    advertisedClaims: [],
    photos: [],
    extractorId: 'manual-entry',
    extraction: {
      confidence: 'low',
      reasons: ['operator-entered manually; no page detection'],
      fieldSources: {
        ...(entry.title.trim() ? { title: 'operator' as const } : {}),
        ...(entry.year !== null ? { year: 'operator' as const } : {}),
        ...(entry.make.trim() ? { make: 'operator' as const } : {}),
        ...(entry.model.trim() ? { model: 'operator' as const } : {}),
        ...(entry.trim.trim() ? { trim: 'operator' as const } : {}),
        ...(entry.description.trim() ? { description: 'operator' as const } : {}),
      },
    },
  };
}

/** Decode an operator-selected image data URL to bytes. Rejects non-image
 * payloads before any upload is attempted. */
export function dataUrlToBlob(dataUrl: string): Blob {
  const match = /^data:(image\/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=]+)$/.exec(dataUrl);
  if (!match) throw new Error('user file is not a base64 image data URL');
  const binary = atob(match[2]);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: match[1].toLowerCase() });
}

/** Strip path components and cap length so an operator-chosen filename is
 * safe to send as a multipart filename. */
export function sanitizeUserFilename(name: string): string {
  const base = name.split(/[\\/]/).pop() ?? '';
  const cleaned = base.replace(/[^A-Za-z0-9._-]+/g, '_').slice(0, 120);
  return cleaned || 'photo';
}

export function toListingPayload(
  listing: ExtractedListing,
  requestedCheck?: RequestedCheck | null,
): ListingPayload {
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
    // Operator-requested check only: never inferred from the page, never
    // merged into advertised claims.
    requested_checks: requestedCheck ? [requestedCheck] : [],
    expected_photo_count: listing.photos.length,
  };
}

function summarize(progress: ImportProgress): void {
  progress.updatedAt = Date.now();
}

/** Map a terminal photo error to a backend failure kind. Auth/permission
 * rejections are permission failures; upload-phase 422s are validation
 * failures; everything else follows the phase it died in. */
export function classifyFailure(err: unknown, phase: 'download' | 'upload'): FailureKind {
  if (err instanceof ExtensionApiError) {
    if (err.status === 401 || err.status === 403) return 'permission';
    if (phase === 'upload' && err.status === 422) return 'validation';
  }
  return phase;
}

function finalize(active: ImportProgress): void {
  const failures = active.photos.filter((p) => p.status === 'failed');
  const stored = active.photos.filter((p) => p.status === 'done' || p.status === 'duplicate');
  if (failures.length === 0) {
    active.status = 'complete';
    active.error = undefined;
  } else if (stored.length === 0) {
    active.status = 'failed';
    active.error = `all ${failures.length} photos failed`;
  } else {
    active.status = 'partial';
    active.error = `${failures.length} of ${active.photos.length} photos failed`;
  }
}

async function runPhoto(
  token: string,
  listingId: string,
  photo: PhotoProgress,
  deps: Required<Pick<ImportDeps, 'fetchImpl' | 'maxAttempts'>> &
    Pick<ImportDeps, 'isCancelled' | 'reportFailure'>,
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
      await reportTerminal(photo, 'upload', new Error('cancelled'), deps.reportFailure, listingId);
      return;
    }
    let phase: 'download' | 'upload' = 'download';
    let downloaded: Blob | null = null;
    try {
      photo.status = 'downloading';
      await checkpoint();
      const { bytes, filename } = await downloadImageBytes(photo.url, deps.fetchImpl);
      if (bytes.size > MAX_DOWNLOAD_BYTES) throw new Error('image exceeds download size limit');
      downloaded = bytes;
      phase = 'upload';
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
        // Upload-phase failures carry the bytes' SHA-256 so the backend
        // merges this report with the validation record it already stored
        // for the same rejected upload. Best-effort: a hashing failure must
        // not suppress the report.
        let photoKey: string | undefined;
        if (phase === 'upload' && downloaded) {
          try {
            photoKey = await sha256HexBytes(downloaded);
          } catch {
            photoKey = undefined;
          }
        }
        await reportTerminal(photo, phase, err, deps.reportFailure, listingId, photoKey);
        return;
      }
      // Whole-image retry; the server dedupes by SHA-256, so a retry after a
      // half-finished attempt cannot duplicate evidence.
    }
  }
}

async function reportTerminal(
  photo: PhotoProgress,
  phase: 'download' | 'upload',
  err: unknown,
  reportFailure: ImportDeps['reportFailure'],
  listingId: string,
  photoKey?: string,
): Promise<void> {
  if (!reportFailure) return;
  try {
    await reportFailure(listingId, {
      sourceUrl: photo.url,
      key: photoKey,
      kind: classifyFailure(err, phase),
      detail: photo.detail ?? 'upload failed',
    });
  } catch {
    // Failure reporting never fails the run.
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

/** Read a stored checkpoint, tolerating the pre-remediation shape that used
 * a `finished` boolean instead of a run status. Anything without a run id,
 * source URL, and photo list is not a checkpoint. */
export function normalizeProgress(raw: unknown): ImportProgress | null {
  if (raw === null || typeof raw !== 'object') return null;
  const progress = raw as ImportProgress & { finished?: boolean };
  if (
    typeof progress.runId !== 'string' ||
    typeof progress.sourceUrl !== 'string' ||
    !Array.isArray(progress.photos)
  ) {
    return null;
  }
  if (
    progress.status === 'active' ||
    progress.status === 'partial' ||
    progress.status === 'complete' ||
    progress.status === 'failed'
  ) {
    return progress as ImportProgress;
  }
  const failures = progress.photos.filter((p) => p.status === 'failed').length;
  const stored = progress.photos.filter(
    (p) => p.status === 'done' || p.status === 'duplicate',
  ).length;
  const status: ImportRunStatus = progress.finished
    ? failures === 0
      ? 'complete'
      : stored === 0
        ? 'failed'
        : 'partial'
    : 'active';
  return { ...(progress as ImportProgress), status };
}

/** Start (or resume) an import run. Resuming reuses the stored listing id and
 * skips photos already done/duplicate, so retries never duplicate evidence.
 * Finished runs (partial/failed/complete) are never resumed: recovery uses
 * `retryFailedPhotos`, and a fresh intent creates a fresh listing.
 *
 * Single-active-import policy: while a run is active, only that same run
 * (same source URL, e.g. resuming after worker suspension) may proceed. A
 * competing import for another listing is rejected before it can create a
 * listing or overwrite the shared checkpoint. Terminal states release the
 * guard, so a suspended worker never locks imports permanently. */
export async function runImport(
  token: string,
  listing: ExtractedListing,
  deps: ImportDeps,
  requestedCheck?: RequestedCheck | null,
): Promise<ImportProgress> {
  const fetchImpl = deps.fetchImpl;
  const maxAttempts = deps.maxAttempts ?? 2;
  const concurrency = deps.concurrency ?? 3;

  const stored = normalizeProgress(await deps.store.load());
  let active: ImportProgress;
  if (stored !== null && stored.status === 'active') {
    if (stored.sourceUrl !== listing.sourceUrl) {
      throw new Error(
        'another import is already running; wait for it to finish or cancel it first',
      );
    }
    active = stored;
  } else {
    let created;
    try {
      created = await createListing(token, toListingPayload(listing, requestedCheck), fetchImpl);
    } catch (err) {
      // A bare "Unauthorized" here means the stored pairing token is
      // unknown to the backend (revoked, or a different token was
      // pasted). Say so plainly instead of surfacing the status text.
      if (err instanceof ExtensionApiError && (err.status === 401 || err.status === 403)) {
        throw new Error(
          'Backend rejected the extension pairing (401). Re-issue a token on the mother-app Extension pairing page, paste it into the extension popup, then retry.',
          { cause: err },
        );
      }
      throw err;
    }
    active = {
      runId: `${Date.now()}`,
      listingId: created.id,
      sourceUrl: listing.sourceUrl,
      photos: listing.photos.map((p) => ({ url: p.url, status: 'pending' as const })),
      startedAt: Date.now(),
      updatedAt: Date.now(),
      status: 'active',
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
      { fetchImpl, maxAttempts, isCancelled: deps.isCancelled, reportFailure: deps.reportFailure },
      checkpoint,
    ),
  );

  finalize(active);
  await checkpoint();
  return active;
}

/** Abandon an inactive checkpoint: transition a stored `active` run to
 * terminal `failed` so a different listing can be imported afterward.
 * Unattempted photos (pending/downloading/uploading markers left behind by
 * a terminated worker) become failed without filing backend failure
 * records — nothing new was attempted, so the backend correctly has no row
 * for them. Already-stored photos, failure details, and the listing itself
 * are untouched, and a later `retryFailedPhotos` can still recover the run
 * under the original listing id. Terminal checkpoints are returned
 * unchanged; with no checkpoint it throws. This must only run when no
 * operation is executing — the background worker checks its in-flight flag
 * first, so abandonment can never interrupt a genuinely active run. */
export async function abandonImport(
  deps: Pick<ImportDeps, 'store'> & Pick<ImportDeps, 'onProgress'>,
): Promise<ImportProgress> {
  const stored = normalizeProgress(await deps.store.load());
  if (!stored || !stored.listingId) throw new Error('no import to abandon');
  if (stored.status !== 'active') return stored;
  for (const photo of stored.photos) {
    if (
      photo.status === 'pending' ||
      photo.status === 'downloading' ||
      photo.status === 'uploading'
    ) {
      photo.status = 'failed';
      photo.detail = 'not attempted (import abandoned)';
    }
  }
  stored.status = 'failed';
  stored.error = 'import abandoned by operator before completion';
  summarize(stored);
  await deps.store.save(stored);
  deps.onProgress?.(stored);
  return stored;
}

/** Retry the failed photos of a finished partial/failed run. Reuses the
 * original persisted listing id; photos already stored are never
 * re-uploaded. Interrupted active runs resume through `runImport`, not here.
 */
export async function retryFailedPhotos(token: string, deps: ImportDeps): Promise<ImportProgress> {
  const stored = normalizeProgress(await deps.store.load());
  if (!stored || !stored.listingId) throw new Error('no import to retry');
  if (stored.status === 'active') {
    throw new Error('import is still running; wait for it to finish first');
  }
  const failed = stored.photos.filter((p) => p.status === 'failed');
  if (failed.length === 0) return stored; // nothing outstanding: idempotent no-op
  for (const photo of failed) {
    photo.status = 'pending';
    photo.detail = undefined;
  }
  stored.status = 'active';
  stored.error = undefined;

  const maxAttempts = deps.maxAttempts ?? 2;
  const concurrency = deps.concurrency ?? 3;
  const checkpoint = async () => {
    summarize(stored);
    await deps.store.save(stored);
    deps.onProgress?.(stored);
  };
  await checkpoint();

  const listingId = stored.listingId;
  await runPool(failed, concurrency, (photo) =>
    runPhoto(
      token,
      listingId,
      photo,
      {
        fetchImpl: deps.fetchImpl,
        maxAttempts,
        isCancelled: deps.isCancelled,
        reportFailure: deps.reportFailure,
      },
      checkpoint,
    ),
  );

  finalize(stored);
  await checkpoint();
  return stored;
}
