import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ListingDetail from './ListingDetail.svelte';

afterEach(() => cleanup());

const listing = {
  id: '11111111-1111-1111-1111-111111111111',
  source_url: 'https://example.test/l/1',
  source_site: 'example.test',
  year: 1987,
  make: 'Test',
  model: 'Coupe',
  trim: 'LX',
  vin: null,
  title: '1987 Test Coupe LX',
  description: '',
  advertised_claims: [],
  expected_photo_count: 2,
  failed_upload_count: 1,
  photo_count: 1,
  import_status: 'partial',
  created_at: '2026-10-29T00:00:00Z',
};

const failures = [
  {
    id: 'f1',
    listing_id: listing.id,
    key: 'url:abc',
    kind: 'download',
    detail: 'image download failed (404)',
    source_url: 'http://127.0.0.1:8901/photos/missing.jpg',
    resolved: false,
    created_at: '2026-10-29T00:00:00Z',
  },
];

function stubFetch(failurePayload: unknown[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url === `/api/listings/${listing.id}`) {
        return new Response(JSON.stringify(listing));
      }
      if (url === `/api/listings/${listing.id}/photos`) {
        return new Response(JSON.stringify([]));
      }
      if (url === `/api/listings/${listing.id}/failed-photos`) {
        return new Response(JSON.stringify(failurePayload));
      }
      if (url === `/api/listings/${listing.id}/audits`) {
        return new Response(JSON.stringify([]));
      }
      throw new Error(`unexpected ${url}`);
    }),
  );
}

describe('ListingDetail import failures', () => {
  it('shows outstanding per-photo failure outcomes for a partial import', async () => {
    stubFetch(failures);
    render(ListingDetail, { props: { id: listing.id } });
    expect(await screen.findByText('partial')).toBeInTheDocument();
    expect(await screen.findByText('Import failures (1)')).toBeInTheDocument();
    expect(await screen.findByText('download failed')).toBeInTheDocument();
    expect(await screen.findByText('— image download failed (404)')).toBeInTheDocument();
    expect(await screen.findByText('http://127.0.0.1:8901/photos/missing.jpg')).toBeInTheDocument();
  });

  it('marks recovered failures while keeping them visible', async () => {
    stubFetch([{ ...failures[0], resolved: true }]);
    render(ListingDetail, { props: { id: listing.id } });
    expect(await screen.findByText('download failed (recovered)')).toBeInTheDocument();
    expect(
      await screen.findByText('All reported failures recovered on retry — evidence is complete.'),
    ).toBeInTheDocument();
  });

  it('hides the failure section when nothing was reported', async () => {
    stubFetch([]);
    render(ListingDetail, { props: { id: listing.id } });
    await screen.findByRole('heading', { name: '1987 Test Coupe LX' });
    expect(screen.queryByText(/Import failures \(/)).not.toBeInTheDocument();
  });

  it('reloads when the routed listing id changes', async () => {
    const other = { ...listing, id: 'other-id', title: 'Other Listing', failed_upload_count: 0 };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === `/api/listings/${listing.id}`) return new Response(JSON.stringify(listing));
        if (url === `/api/listings/other-id`) return new Response(JSON.stringify(other));
        if (url.endsWith('/photos')) return new Response(JSON.stringify([]));
        if (url.endsWith('/failed-photos')) {
          return new Response(JSON.stringify(url.includes('other-id') ? [] : failures));
        }
        throw new Error(`unexpected ${url}`);
      }),
    );
    const view = render(ListingDetail, { props: { id: listing.id } });
    await screen.findByText('Import failures (1)');
    await view.rerender({ id: 'other-id' });
    await screen.findByRole('heading', { name: 'Other Listing' });
    expect(screen.queryByText(/Import failures \(/)).not.toBeInTheDocument();
  });
});
