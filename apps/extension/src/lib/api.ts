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

function ipv4Octets(host: string): [number, number, number, number] | null {
  const match = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!match) return null;
  const octets = match.slice(1, 5).map(Number) as [number, number, number, number];
  if (octets.some((o) => o > 255)) return null;
  return octets;
}

/** True for every IPv4 range that is not a public destination: loopback,
 * private, link-local, unspecified, carrier-grade NAT, documentation,
 * benchmarking, multicast, and reserved space. */
function isNonPublicIPv4(octets: [number, number, number, number]): boolean {
  const [a, b] = octets;
  if (a === 0) return true; // "this network" / unspecified
  if (a === 10) return true; // private
  if (a === 100 && b >= 64 && b <= 127) return true; // carrier-grade NAT
  if (a === 127) return true; // full loopback /8 (not just 127.0.0.1)
  if (a === 169 && b === 254) return true; // link-local
  if (a === 172 && b >= 16 && b <= 31) return true; // private
  if (a === 192 && b === 0) return true; // IETF protocol assignments incl. 192.0.0.170/.171
  if (a === 192 && b === 2) return true; // documentation (TEST-NET-1)
  if (a === 192 && b === 168) return true; // private
  if (a === 198 && (b === 18 || b === 19)) return true; // benchmarking
  if (a === 198 && b === 51 && octets[2] === 100) return true; // documentation
  if (a === 203 && b === 0 && octets[2] === 113) return true; // documentation
  if (a >= 224 && a <= 239) return true; // multicast
  if (a >= 240) return true; // reserved
  return false;
}

function expandIPv6(host: string): number[] | null {
  // Parse an IPv6 literal into eight 16-bit groups. Returns null when the
  // literal is malformed (fail closed: callers treat null as disallowed).
  if (host.includes('::')) {
    const [head, tail, extra] = host.split('::');
    if (extra !== undefined) return null;
    const headParts = head === '' ? [] : head.split(':');
    const tailParts = tail === '' ? [] : tail.split(':');
    if (headParts.length + tailParts.length > 7) return null;
    const missing = 8 - headParts.length - tailParts.length;
    const parts = [...headParts, ...Array<string>(missing).fill('0'), ...tailParts];
    return parseIPv6Parts(parts);
  }
  return parseIPv6Parts(host.split(':'));
}

function parseIPv6Parts(parts: string[]): number[] | null {
  if (parts.length !== 8) return null;
  const groups: number[] = [];
  for (const part of parts) {
    if (!/^[0-9a-fA-F]{1,4}$/.test(part)) return null;
    groups.push(parseInt(part, 16));
  }
  return groups;
}

/** True for non-public IPv6 destinations: loopback, unspecified,
 * link-local, unique-local, documentation, and multicast. IPv4-mapped
 * addresses delegate to the IPv4 classifier so `::ffff:127.0.0.2` cannot
 * smuggle a loopback target past the check. */
function isNonPublicIPv6(host: string): boolean {
  const groups = expandIPv6(host);
  if (!groups) return true; // malformed literal: fail closed
  const [g0, g1, , , , , g6, g7] = groups;
  if (groups.every((g) => g === 0)) return true; // ::
  if (g0 === 0 && g1 === 0 && groups.slice(2, 7).every((g) => g === 0) && g7 === 1) {
    return true; // ::1 loopback
  }
  if ((g0 & 0xffc0) === 0xfe80) return true; // fe80::/10 link-local
  if ((g0 & 0xfe00) === 0xfc00) return true; // fc00::/7 unique-local
  if ((g0 & 0xff00) === 0xff00) return true; // ff00::/8 multicast
  if (g0 === 0x2001 && g1 === 0x0db8) return true; // 2001:db8::/32 documentation
  // ::ffff:0:0/96 IPv4-mapped: classify the embedded IPv4 address, so a
  // hex-encoded mapped loopback cannot smuggle past the IPv4 check. (Dotted
  // forms like ::ffff:127.0.0.2 never parse as hex groups, so they already
  // failed closed above.)
  if (groups.slice(0, 6).every((g, i) => g === (i === 5 ? 0xffff : 0))) {
    const dotted = `${g6 >> 8}.${g6 & 0xff}.${g7 >> 8}.${g7 & 0xff}`;
    const octets = ipv4Octets(dotted);
    return octets ? isNonPublicIPv4(octets) : true;
  }
  return false;
}

function isPrivateHostname(hostname: string): boolean {
  let host = hostname.toLowerCase();
  // Strip brackets when the caller passes a bracketed literal.
  if (host.startsWith('[') && host.endsWith(']')) host = host.slice(1, -1);
  if (host === 'localhost' || host.endsWith('.localhost')) return true;
  const octets = ipv4Octets(host);
  if (octets) return isNonPublicIPv4(octets);
  if (host.includes(':')) return isNonPublicIPv6(host);
  // Single-label or unresolved names are not matched here; DNS resolution is
  // a browser-enforced limitation (see docs/security.md). Public registrable
  // names pass; the fixture exception and host_permissions gate the rest.
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

export interface FailureReportPayload {
  sourceUrl: string | null;
  kind: 'download' | 'permission' | 'validation' | 'upload';
  detail: string;
}

export interface ReportedFailure {
  id: string;
  key: string;
  kind: string;
  resolved: boolean;
}

/** Report a photo that never reached the upload endpoint (download or
 * permission failure). Idempotent server-side. Throws on transport errors;
 * the import orchestrator reports best-effort and never fails a run over it. */
export async function reportFailure(
  token: string,
  listingId: string,
  failure: FailureReportPayload,
  fetchImpl: typeof fetch = fetch,
): Promise<ReportedFailure> {
  const res = await fetchImpl(
    `${LOCAL_API_BASE}/api/extension/listings/${listingId}/failed-photos`,
    {
      method: 'POST',
      headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_url: failure.sourceUrl,
        kind: failure.kind,
        detail: failure.detail,
      }),
    },
  );
  if (!res.ok) throw new ExtensionApiError(res.status, await readError(res));
  return (await res.json()) as ReportedFailure;
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

export const MAX_IMAGE_REDIRECTS = 3;

function isRedirectStatus(status: number): boolean {
  return status === 301 || status === 302 || status === 303 || status === 307 || status === 308;
}

/** Follow redirects manually so every hop is validated against the same
 * network policy as the original URL. Never sends credentials: image
 * downloads carry no auth headers on any hop (the pairing token is only
 * ever attached to localhost API calls). */
async function fetchImageWithRedirects(
  url: string,
  fetchImpl: typeof fetch,
  signal: AbortSignal,
): Promise<{ res: Response; finalUrl: string }> {
  let current = url;
  for (let hop = 0; hop <= MAX_IMAGE_REDIRECTS; hop += 1) {
    const res = await fetchImpl(current, { signal, redirect: 'manual' });
    if (!isRedirectStatus(res.status)) return { res, finalUrl: current };
    if (hop === MAX_IMAGE_REDIRECTS) {
      throw new ExtensionApiError(0, 'image redirect limit exceeded');
    }
    const location = res.headers.get('location');
    if (!location) throw new ExtensionApiError(0, 'image redirect without location');
    let next: URL;
    try {
      next = new URL(location, current);
    } catch {
      throw new ExtensionApiError(0, 'image redirect has an invalid location');
    }
    if (!isAllowedImageUrl(next.toString())) {
      throw new ExtensionApiError(0, `refusing image redirect to disallowed URL (${next.host})`);
    }
    current = next.toString();
  }
  throw new ExtensionApiError(0, 'image redirect limit exceeded');
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
    const { res, finalUrl } = await fetchImageWithRedirects(url, fetchImpl, controller.signal);
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
    const filename = new URL(finalUrl).pathname.split('/').pop() || 'photo';
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
