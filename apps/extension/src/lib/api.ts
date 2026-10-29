/** Localhost API client (background/popup only — never the content script).
 *
 * The pairing token lives in trusted extension storage and is attached here.
 * Image downloads are allowlisted: public http(s) hosts plus the explicit
 * fixture-server exception. Page-supplied URLs are never routed to other
 * private/local targets.
 */

// Build-time override (VITE_VCC_API_BASE) exists for port conflicts only;
// production builds use the loopback default below.
export const LOCAL_API_BASE = import.meta.env.VITE_VCC_API_BASE ?? 'http://127.0.0.1:8000';
export const MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024; // headroom over the 15 MiB server cap
export const FETCH_TIMEOUT_MS = 30_000;

export class ExtensionApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function isPrivateHostname(hostname: string): boolean {
  const host = hostname.toLowerCase();
  if (host === 'localhost' || host.endsWith('.localhost')) return true;
  if (host === '127.0.0.1' || host === '::1' || host === '[::1]') return true;
  const v4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (v4) {
    const [, a, b] = v4.map(Number);
    if (a === 10) return true;
    if (a === 172 && b >= 16 && b <= 31) return true;
    if (a === 192 && b === 168) return true;
    if (a === 169 && b === 254) return true;
    return false;
  }
  if (host.includes(':')) return true; // conservative: no literal IPv6 remotes
  return false;
}

/** Explicit exceptions to the no-private-fetch rule (E2E fixture server). */
const IMAGE_URL_EXCEPTIONS = new Set(['127.0.0.1:8901']);

export function isAllowedImageUrl(rawUrl: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return false;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
  const authority = parsed.host.toLowerCase();
  if (IMAGE_URL_EXCEPTIONS.has(authority)) return true;
  return !isPrivateHostname(parsed.hostname);
}

export interface ListingPayload {
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
}

export interface CreatedListing {
  id: string;
  import_status: string;
}

export interface UploadedPhoto {
  id: string;
  sha256: string;
  duplicate: boolean;
}

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === 'string') return body.detail;
  } catch {
    // fall through to generic message
  }
  return `localhost API request failed (${res.status})`;
}

export function authHeaders(token: string): Record<string, string> {
  return { 'X-Extension-Token': token };
}

export async function checkHealth(fetchImpl: typeof fetch = fetch): Promise<boolean> {
  try {
    const res = await fetchImpl(`${LOCAL_API_BASE}/api/health`);
    return res.ok;
  } catch {
    return false;
  }
}

export async function createListing(
  token: string,
  payload: ListingPayload,
  fetchImpl: typeof fetch = fetch,
): Promise<CreatedListing> {
  const res = await fetchImpl(`${LOCAL_API_BASE}/api/extension/listings`, {
    method: 'POST',
    headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new ExtensionApiError(res.status, await readError(res));
  return (await res.json()) as CreatedListing;
}

export async function uploadPhoto(
  token: string,
  listingId: string,
  bytes: Blob,
  filename: string,
  sourceUrl: string | null,
  fetchImpl: typeof fetch = fetch,
): Promise<UploadedPhoto> {
  const form = new FormData();
  form.append('file', bytes, filename);
  if (sourceUrl) form.append('source_url', sourceUrl);
  const res = await fetchImpl(`${LOCAL_API_BASE}/api/extension/listings/${listingId}/photos`, {
    method: 'POST',
    headers: authHeaders(token),
    body: form,
  });
  if (!res.ok) throw new ExtensionApiError(res.status, await readError(res));
  return (await res.json()) as UploadedPhoto;
}

export async function downloadImageBytes(
  url: string,
  fetchImpl: typeof fetch = fetch,
): Promise<{ bytes: Blob; filename: string }> {
  if (!isAllowedImageUrl(url)) {
    throw new ExtensionApiError(0, `refusing to fetch disallowed image URL (${new URL(url).host})`);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetchImpl(url, { signal: controller.signal, redirect: 'follow' });
    if (!res.ok) throw new ExtensionApiError(res.status, `image download failed (${res.status})`);
    const declared = Number(res.headers.get('content-length') ?? '0');
    if (declared > MAX_DOWNLOAD_BYTES) {
      throw new ExtensionApiError(413, 'image exceeds download size limit');
    }
    // Stream with a hard cap: a lying/absent Content-Length cannot OOM the worker.
    const reader = res.body?.getReader();
    if (!reader) throw new ExtensionApiError(0, 'image response has no body');
    const chunks: BlobPart[] = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_DOWNLOAD_BYTES) {
        await reader.cancel().catch(() => undefined);
        throw new ExtensionApiError(413, 'image exceeds download size limit');
      }
      chunks.push(value);
    }
    const bytes = new Blob(chunks, {
      type: res.headers.get('content-type') ?? 'application/octet-stream',
    });
    const filename = new URL(res.url || url).pathname.split('/').pop() || 'photo';
    return { bytes, filename };
  } catch (err) {
    if (err instanceof ExtensionApiError) throw err;
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ExtensionApiError(0, 'image download timed out');
    }
    throw new ExtensionApiError(0, err instanceof Error ? err.message : 'image download failed');
  } finally {
    clearTimeout(timer);
  }
}
