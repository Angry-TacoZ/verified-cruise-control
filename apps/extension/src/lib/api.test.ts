import { describe, expect, it, vi } from 'vitest';
import {
  authHeaders,
  createListing,
  downloadImageBytes,
  isAllowedImageUrl,
  uploadPhoto,
} from './api';

describe('image URL allowlist', () => {
  it('allows public hosts and the fixture server', () => {
    expect(isAllowedImageUrl('https://cdn.example.test/p.jpg')).toBe(true);
    expect(isAllowedImageUrl('http://127.0.0.1:8901/photos/p.jpg')).toBe(true);
  });

  it('blocks non-http schemes', () => {
    expect(isAllowedImageUrl('javascript:alert(1)')).toBe(false);
    expect(isAllowedImageUrl('file:///etc/passwd')).toBe(false);
    expect(isAllowedImageUrl('data:image/png;base64,xx')).toBe(false);
  });

  it('blocks private/local targets outside the fixture exception', () => {
    expect(isAllowedImageUrl('http://127.0.0.1:8000/p.jpg')).toBe(false);
    expect(isAllowedImageUrl('http://localhost/p.jpg')).toBe(false);
    expect(isAllowedImageUrl('http://10.0.0.5/p.jpg')).toBe(false);
    expect(isAllowedImageUrl('http://192.168.1.1/p.jpg')).toBe(false);
    expect(isAllowedImageUrl('http://169.254.169.254/latest')).toBe(false);
    expect(isAllowedImageUrl('not a url')).toBe(false);
  });
});

describe('localhost API client', () => {
  it('attaches the pairing token header', () => {
    expect(authHeaders('tok')).toEqual({ 'X-Extension-Token': 'tok' });
  });

  it('creates listings with JSON + auth header', async () => {
    const fetchMock = vi.fn(
      async () => new Response(JSON.stringify({ id: 'L1' }), { status: 201 }),
    );
    const created = await createListing(
      'tok',
      {
        source_url: 'http://127.0.0.1:8901/l/1',
        source_site: 's',
        year: null,
        make: '',
        model: '',
        trim: '',
        vin: null,
        title: 't',
        description: '',
        advertised_claims: [],
        expected_photo_count: 1,
      },
      fetchMock as unknown as typeof fetch,
    );
    expect(created.id).toBe('L1');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8000/api/extension/listings');
    expect((init.headers as Record<string, string>)['X-Extension-Token']).toBe('tok');
  });

  it('uploads one photo per multipart request', async () => {
    const fetchMock = vi.fn(
      async () => new Response(JSON.stringify({ id: 'P1', sha256: 'x', duplicate: false })),
    );
    await uploadPhoto(
      'tok',
      'L1',
      new Blob(['bytes'], { type: 'image/png' }),
      'front.jpg',
      'http://127.0.0.1:8901/front.jpg',
      fetchMock as unknown as typeof fetch,
    );
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8000/api/extension/listings/L1/photos');
    expect(init.body).toBeInstanceOf(FormData);
    const form = init.body as FormData;
    expect((form.get('file') as File).name).toBe('front.jpg');
    expect(form.get('source_url')).toBe('http://127.0.0.1:8901/front.jpg');
  });

  it('surfaces server errors with status', async () => {
    const fetchMock = vi.fn(
      async () => new Response(JSON.stringify({ detail: 'bad' }), { status: 422 }),
    );
    await expect(
      createListing('tok', {} as never, fetchMock as unknown as typeof fetch),
    ).rejects.toMatchObject({ status: 422 });
  });

  it('refuses to download disallowed URLs without fetching', async () => {
    const fetchMock = vi.fn();
    await expect(
      downloadImageBytes('http://169.254.169.254/x', fetchMock as unknown as typeof fetch),
    ).rejects.toThrow(/disallowed/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects oversized downloads from Content-Length', async () => {
    const fetchMock = vi.fn(
      async () => new Response('x', { headers: { 'content-length': String(100 * 1024 * 1024) } }),
    );
    await expect(
      downloadImageBytes('https://cdn.example.test/big.jpg', fetchMock as unknown as typeof fetch),
    ).rejects.toMatchObject({ status: 413 });
  });
});
