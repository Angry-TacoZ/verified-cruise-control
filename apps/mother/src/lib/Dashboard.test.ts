import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import Dashboard from './Dashboard.svelte';

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
  failed_upload_count: 0,
  photo_count: 2,
  import_status: 'complete',
  created_at: '2026-10-29T00:00:00Z',
};

describe('Dashboard', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ items: [listing], count: 1 }))),
    );
  });

  it('lists stored listings with counts and statuses', async () => {
    render(Dashboard, { props: { gateSetup: false } });
    expect(await screen.findByText('1987 Test Coupe LX')).toBeInTheDocument();
    expect(await screen.findByText('2 photos')).toBeInTheDocument();
    expect(await screen.findByText('complete')).toBeInTheDocument();
    expect(await screen.findByText(/1 stored listing/)).toBeInTheDocument();
  });

  it('links each listing to its detail route', async () => {
    render(Dashboard, { props: { gateSetup: false } });
    const link = await screen.findByRole('link', { name: /1987 Test Coupe LX/ });
    expect(link).toHaveAttribute('href', `#/listings/${listing.id}`);
  });

  it('gates on setup when the operator has not initialized', async () => {
    render(Dashboard, { props: { gateSetup: true } });
    expect(await screen.findByText(/Finish/)).toBeInTheDocument();
    expect(await screen.findByRole('link', { name: 'operator setup' })).toHaveAttribute(
      'href',
      '#/setup',
    );
  });

  it('shows the empty state when no listings exist', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ items: [], count: 0 }))),
    );
    render(Dashboard, { props: { gateSetup: false } });
    expect(await screen.findByText(/No listings yet/)).toBeInTheDocument();
  });

  it('shows an error with retry on backend failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('boom', { status: 500 })),
    );
    render(Dashboard, { props: { gateSetup: false } });
    expect(await screen.findByText(/Request failed/)).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });
});
