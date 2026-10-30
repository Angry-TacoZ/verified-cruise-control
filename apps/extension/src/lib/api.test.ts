import { describe, expect, it, vi } from 'vitest';
import {
  authHeaders,
  createListing,
  downloadImageBytes,
  isAllowedImageUrl,
  MAX_IMAGE_REDIRECTS,
  reportFailure,
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

  it('rejects the full loopback range, not just 127.0.0.1', () => {
    for (const host of ['127.0.0.1', '127.0.0.2', '127.1.2.3', '127.255.255.255']) {
      expect(isAllowedImageUrl(`http://${host}/p.jpg`)).toBe(false);
    }
  });

  it('rejects reserved and special-use IPv4 ranges', () => {
    const blocked = [
      '0.0.0.0',
      '10.0.0.1',
      '100.64.0.1',
      '100.127.255.254',
      '169.254.169.254',
      '172.16.0.1',
      '172.31.255.255',
      '192.0.0.170',
      '192.0.2.1',
      '192.168.1.1',
      '198.18.0.1',
      '198.51.100.1',
      '203.0.113.1',
      '224.0.0.1',
      '240.0.0.1',
    ];
    for (const host of blocked) {
      expect(isAllowedImageUrl(`http://${host}/p.jpg`)).toBe(false);
    }
    // Adjacent public space stays reachable.
    expect(isAllowedImageUrl('http://11.0.0.1/p.jpg')).toBe(true);
    expect(isAllowedImageUrl('http://172.15.255.255/p.jpg')).toBe(true);
    expect(isAllowedImageUrl('http://172.32.0.1/p.jpg')).toBe(true);
  });

  it('rejects non-public IPv6 literals, including mapped loopback', () => {
    const blocked = [
      '[::1]',
      '[::]',
      '[fe80::1]',
      '[febf:ffff::1]',
      '[fc00::1]',
      '[fd12:3456::1]',
      '[ff02::1]',
      '[2001:db8::1]',
      '[::ffff:7f00:2]',
      '[::ffff:0a00:1]',
    ];
    for (const host of blocked) {
      expect(isAllowedImageUrl(`http://${host}/p.jpg`)).toBe(false);
    }
    // Hex-encoded mapped public IPv4 and public global unicast stay reachable.
    expect(isAllowedImageUrl('http://[::ffff:808:808]/p.jpg')).toBe(true);
    expect(isAllowedImageUrl('http://[2606:4700:4700::1111]/p.jpg')).toBe(true);
  });

  it('rejects localhost spellings', () => {
    expect(isAllowedImageUrl('http://localhost:8902/p.jpg')).toBe(false);
    expect(isAllowedImageUrl('http://foo.localhost/p.jpg')).toBe(false);
  });
});

function imageResponse(body = 'bytes'): Response {
  return new Response(body, {
    headers: { 'content-type': 'image/jpeg', 'content-length': String(body.length) },
  });
}

function redirectResponse(location: string | null): Response {
  const headers: Record<string, string> = {};
  if (location !== null) headers['location'] = location;
  return new Response(null, { status: 302, headers });
}

describe('image redirect policy', () => {
  it('refuses a redirect from public to localhost without a second fetch', async () => {
    const fetchMock = vi.fn(async (url: string | URL | Request) => {
      const href = String(url);
      if (href === 'https://cdn.example.test/start.jpg') {
        return redirectResponse('http://127.0.0.1:8000/evil.jpg');
      }
      throw new Error(`unexpected fetch of ${href}`);
    });
    await expect(
      downloadImageBytes(
        'https://cdn.example.test/start.jpg',
        fetchMock as unknown as typeof fetch,
      ),
    ).rejects.toThrow(/disallowed/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('refuses a redirect from public to a private address', async () => {
    const fetchMock = vi.fn(async (url: string | URL | Request) => {
      const href = String(url);
      if (href === 'https://cdn.example.test/start.jpg') {
        return redirectResponse('http://10.0.0.1/internal.jpg');
      }
      throw new Error(`unexpected fetch of ${href}`);
    });
    await expect(
      downloadImageBytes(
        'https://cdn.example.test/start.jpg',
        fetchMock as unknown as typeof fetch,
      ),
    ).rejects.toThrow(/disallowed/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('follows a benign redirect chain and names the file from the final URL', async () => {
    const fetchMock = vi.fn(async (url: string | URL | Request) => {
      const href = String(url);
      if (href === 'https://cdn.example.test/a.jpg') return redirectResponse('/b.jpg');
      if (href === 'https://cdn.example.test/b.jpg') {
        return redirectResponse('https://img.example.test/final.jpg');
      }
      if (href === 'https://img.example.test/final.jpg') return imageResponse('photo-bytes');
      throw new Error(`unexpected fetch of ${href}`);
    });
    const { bytes, filename } = await downloadImageBytes(
      'https://cdn.example.test/a.jpg',
      fetchMock as unknown as typeof fetch,
    );
    expect(filename).toBe('final.jpg');
    expect(await bytes.text()).toBe('photo-bytes');
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('enforces the redirect limit on chains and loops', async () => {
    const fetchMock = vi.fn(async () => redirectResponse('https://cdn.example.test/loop.jpg'));
    await expect(
      downloadImageBytes('https://cdn.example.test/loop.jpg', fetchMock as unknown as typeof fetch),
    ).rejects.toThrow(/redirect limit/);
    expect(fetchMock).toHaveBeenCalledTimes(MAX_IMAGE_REDIRECTS + 1);
  });

  it('rejects redirects without a usable location', async () => {
    const noLocation = vi.fn(async () => redirectResponse(null));
    await expect(
      downloadImageBytes('https://cdn.example.test/x.jpg', noLocation as unknown as typeof fetch),
    ).rejects.toThrow(/without location/);
    const badLocation = vi.fn(async () => redirectResponse('http://::not-a-url'));
    await expect(
      downloadImageBytes('https://cdn.example.test/x.jpg', badLocation as unknown as typeof fetch),
    ).rejects.toThrow(/disallowed|invalid location/);
  });

  it('sends no credentials on image downloads or redirect hops', async () => {
    const seen: unknown[] = [];
    const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      seen.push(init?.headers);
      const href = String(url);
      if (href === 'https://cdn.example.test/a.jpg') return redirectResponse('/b.jpg');
      return imageResponse();
    });
    await downloadImageBytes(
      'https://cdn.example.test/a.jpg',
      fetchMock as unknown as typeof fetch,
    );
    expect(seen.length).toBe(2);
    for (const headers of seen) {
      expect(headers ?? {}).not.toMatchObject({ 'X-Extension-Token': expect.anything() });
    }
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

  it('reports download failures with the pairing token', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ id: 'F1', key: 'k', kind: 'download', resolved: false }), {
          status: 201,
        }),
    );
    const reported = await reportFailure(
      'tok',
      'L1',
      {
        sourceUrl: 'https://cdn.example.test/missing.jpg',
        kind: 'download',
        detail: 'image download failed (404)',
      },
      fetchMock as unknown as typeof fetch,
    );
    expect(reported.kind).toBe('download');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8000/api/extension/listings/L1/failed-photos');
    expect((init.headers as Record<string, string>)['X-Extension-Token']).toBe('tok');
    expect(JSON.parse(String(init.body))).toMatchObject({
      source_url: 'https://cdn.example.test/missing.jpg',
      kind: 'download',
    });
  });

  it('passes the rejected-bytes hash through failure reports', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ id: 'F1', key: 'k', kind: 'validation', resolved: false }), {
          status: 201,
        }),
    );
    await reportFailure(
      'tok',
      'L1',
      {
        sourceUrl: 'https://cdn.example.test/bad.jpg',
        key: 'abc123',
        kind: 'validation',
        detail: 'format: cannot identify image file',
      },
      fetchMock as unknown as typeof fetch,
    );
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({
      source_url: 'https://cdn.example.test/bad.jpg',
      key: 'abc123',
      kind: 'validation',
    });
  });
});
