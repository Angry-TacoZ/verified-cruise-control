import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ListingDetail from './ListingDetail.svelte';

afterEach(() => cleanup());

const listing = {
  id: 'l1',
  source_url:
    'https://example.test/l/1?inclusionType=NATIONWIDE_SHIPPING&sponsoredType=NONE&extra=abcdefghij',
  source_site: 'example.test',
  year: 2021,
  make: 'Test',
  model: 'Hybrid',
  trim: 'LX',
  vin: null,
  title: '2021 Test Hybrid LX',
  description: '',
  advertised_claims: ['VIN WMWZB3C57R7N12345', 'Bluetooth'],
  expected_photo_count: 3,
  failed_upload_count: 2,
  photo_count: 1,
  import_status: 'partial',
  created_at: '2026-10-29T00:00:00Z',
};

const photos = [
  {
    id: 'p1',
    listing_id: 'l1',
    source_url: 'https://example.test/p1.jpg',
    sha256: 'abc123def456abc123def456abc123def456abc123def456abc123def456abcd',
    original_filename: 'p1.jpg',
    file_size: 1234,
    mime_type: 'image/jpeg',
    image_format: 'JPEG',
    width: 800,
    height: 600,
    provenance: 'listing',
    upload_status: 'stored',
  },
];

const failures = [
  {
    id: 'f1',
    kind: 'download',
    detail: 'host blocked the request',
    source_url: 'https://cdn.test/a.jpg',
    resolved: false,
  },
  { id: 'f2', kind: 'permission', detail: '', source_url: '', resolved: true },
];

function stubFetch(failureRows: unknown[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      const u = String(url);
      if (u.endsWith('/failed-photos')) return new Response(JSON.stringify(failureRows));
      if (u.endsWith('/photos')) return new Response(JSON.stringify(photos));
      return new Response(JSON.stringify(listing));
    }),
  );
}

describe('ListingDetail import failures', () => {
  it('summarizes outstanding failures and collapses per-photo rows', async () => {
    stubFetch(failures);
    render(ListingDetail, { props: { id: 'l1' } });
    expect(await screen.findByText('Import failures (2)')).toBeInTheDocument();
    expect(await screen.findByText(/1 photo couldn't be/)).toBeInTheDocument();
    expect(await screen.findByText(/runs on the 1 available/)).toBeInTheDocument();
    const details = screen.getByText('Show per-photo details').closest('details');
    expect(details).not.toBeNull();
    expect(details!.textContent).toMatch(/download failed/);
    expect(details!.textContent).toMatch(/host permission denied \(recovered\)/);
    expect(details!.textContent).toMatch(/cdn\.test\/a\.jpg/);
  });

  it('truncates the source URL but keeps the full link, and shows the claim VIN', async () => {
    stubFetch([]);
    render(ListingDetail, { props: { id: 'l1' } });
    const link = await screen.findByRole('link', { name: /example\.test/ });
    expect(link.textContent).toMatch(/\u2026$/);
    expect(link.textContent!.length).toBeLessThan(listing.source_url.length);
    expect(link.getAttribute('href')).toBe(listing.source_url);
    expect(link.getAttribute('title')).toBe(listing.source_url);
    expect(screen.getByText('WMWZB3C57R7N12345')).toBeInTheDocument();
  });

  it('reports a fully recovered import without the warning tone', async () => {
    stubFetch([{ ...failures[1] }]);
    render(ListingDetail, { props: { id: 'l1' } });
    expect(await screen.findByText('Import failures (1)')).toBeInTheDocument();
    expect(await screen.findByText(/evidence is complete/)).toBeInTheDocument();
    expect(screen.queryByText(/couldn't be/)).not.toBeInTheDocument();
  });
});
