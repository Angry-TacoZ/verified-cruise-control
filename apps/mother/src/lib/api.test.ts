import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, backend, resetCsrfForTests } from './api';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('api client', () => {
  beforeEach(() => {
    resetCsrfForTests();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/csrf') return jsonResponse({ csrf_token: 'csrf-123' });
        if (url === '/api/health') return jsonResponse({ status: 'ok' });
        throw new Error(`unexpected ${url}`);
      }),
    );
  });

  it('sends GETs without a CSRF bootstrap', async () => {
    await backend.health();
    const calls = (fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls;
    expect(calls).toHaveLength(1);
    expect(calls[0][0]).toBe('/api/health');
  });

  it('bootstraps CSRF once and sends the token on POSTs', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (url: string) => {
      if (url === '/api/csrf') return jsonResponse({ csrf_token: 'csrf-123' });
      if (url === '/api/setup/initialize')
        return jsonResponse({ pairing_id: 'p', extension_token: 't' });
      if (url === '/api/setup/logout') return jsonResponse({ ok: true });
      throw new Error(`unexpected ${url}`);
    });
    await backend.initialize('setup-abc');
    await backend.logout();
    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls;
    const csrfCalls = calls.filter(([url]) => url === '/api/csrf');
    expect(csrfCalls).toHaveLength(1);
    const init = calls.find(([url]) => url === '/api/setup/initialize');
    expect((init?.[1]?.headers as Record<string, string>)['X-CSRFToken']).toBe('csrf-123');
  });

  it('raises ApiError with status and server detail', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (url: string) => {
      if (url === '/api/csrf') return jsonResponse({ csrf_token: 'csrf-123' });
      return jsonResponse({ detail: 'vin must be 17 chars' }, 422);
    });
    const err = await backend
      .createListing({ source_url: 'x', source_site: 'y', title: 't' })
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(422);
    expect((err as ApiError).message).toContain('vin must be 17 chars');
  });

  it('builds photo URLs without exposing storage paths', () => {
    expect(backend.photoUrl('listing-1', 'photo-2')).toBe('/api/listings/listing-1/photos/photo-2');
  });
});
