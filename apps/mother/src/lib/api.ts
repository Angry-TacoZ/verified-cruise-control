/** Typed client for the mother-app API boundary (session + CSRF). */

export interface Listing {
  id: string;
  source_url: string;
  source_site: string;
  year: number | null;
  make: string;
  model: string;
  trim: string;
  vin: string | null;
  title: string;
  description: string;
  advertised_claims: string[];
  expected_photo_count: number | null;
  failed_upload_count: number;
  photo_count: number;
  import_status: string;
  created_at: string;
}

export interface ListingInput {
  source_url: string;
  source_site: string;
  year?: number | null;
  make?: string;
  model?: string;
  trim?: string;
  vin?: string | null;
  title: string;
  description?: string;
  advertised_claims?: string[];
  expected_photo_count?: number | null;
}

export interface Photo {
  id: string;
  listing_id: string;
  source_url: string | null;
  sha256: string;
  original_filename: string;
  file_size: number;
  mime_type: string;
  image_format: string;
  width: number;
  height: number;
  provenance: string;
  upload_status: string;
  duplicate: boolean;
  created_at: string;
}

export interface PhotoFailure {
  id: string;
  listing_id: string;
  key: string;
  kind: 'download' | 'permission' | 'validation' | 'upload';
  detail: string;
  source_url: string | null;
  resolved: boolean;
  created_at: string;
}

export interface PairingInfo {
  id: string;
  label: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  active: boolean;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

let csrfToken: string | null = null;

async function ensureCsrf(): Promise<string> {
  if (csrfToken) return csrfToken;
  const res = await fetch('/api/csrf', { credentials: 'same-origin' });
  if (!res.ok) throw new ApiError(res.status, 'CSRF bootstrap failed');
  const body = (await res.json()) as { csrf_token: string };
  csrfToken = body.csrf_token;
  return csrfToken;
}

async function request<T>(
  path: string,
  options: { method?: string; json?: unknown; form?: FormData } = {},
): Promise<T> {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = {};
  let body: BodyInit | undefined;
  if (options.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(options.json);
  } else if (options.form !== undefined) {
    body = options.form;
  }
  if (method !== 'GET' && method !== 'HEAD') {
    headers['X-CSRFToken'] = await ensureCsrf();
  }
  const res = await fetch(path, { method, headers, body, credentials: 'same-origin' });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    // Non-JSON body: data stays null and the generic message is used.
  }
  if (!res.ok) {
    const detail =
      (data as { detail?: unknown } | null)?.detail ??
      (data as { message?: unknown } | null)?.message;
    throw new ApiError(
      res.status,
      typeof detail === 'string' ? detail : `Request failed (${res.status})`,
    );
  }
  return data as T;
}

export function resetCsrfForTests(): void {
  csrfToken = null;
}

export const backend = {
  health: () => request<{ status: string; service: string; api_version: string }>('/api/health'),
  setupStatus: () => request<{ initialized: boolean }>('/api/setup/status'),
  initialize: (setup_token: string) =>
    request<{ pairing_id: string | null; extension_token: string | null; fresh: boolean }>(
      '/api/setup/initialize',
      { method: 'POST', json: { setup_token } },
    ),
  logout: () => request<{ ok: boolean }>('/api/setup/logout', { method: 'POST', json: {} }),
  listListings: () => request<{ items: Listing[]; count: number }>('/api/listings'),
  getListing: (id: string) => request<Listing>(`/api/listings/${id}`),
  listPhotos: (listingId: string) => request<Photo[]>(`/api/listings/${listingId}/photos`),
  listFailures: (listingId: string) =>
    request<PhotoFailure[]>(`/api/listings/${listingId}/failed-photos`),
  createListing: (input: ListingInput) =>
    request<Listing>('/api/listings', { method: 'POST', json: input }),
  uploadPhoto: (listingId: string, file: File, sourceUrl?: string) => {
    const form = new FormData();
    form.append('file', file);
    if (sourceUrl) form.append('source_url', sourceUrl);
    return request<Photo>(`/api/listings/${listingId}/photos`, { method: 'POST', form });
  },
  pairingStatus: () => request<PairingInfo[]>('/api/pairing/status'),
  rotatePairing: (label = '') =>
    request<{ pairing_id: string; extension_token: string; fresh: boolean }>(
      '/api/pairing/rotate',
      { method: 'POST', json: { label } },
    ),
  revokePairing: (id: string) =>
    request<{ ok: boolean }>(`/api/pairing/${id}/revoke`, { method: 'POST', json: {} }),
  photoUrl: (listingId: string, photoId: string) => `/api/listings/${listingId}/photos/${photoId}`,
};
