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

  it('shows ACC badges for audited, running, and pending checks', async () => {
    const audited = {
      ...listing,
      id: '22222222-2222-2222-2222-222222222222',
      requested_checks: ['adaptive_cruise_control'],
      latest_audit: {
        job_id: '33333333-3333-3333-3333-333333333333',
        status: 'complete',
        current_stage: 'done',
        acc_state: 'visually_supported',
        spend_usd: '0.0100',
        model_calls_made: 2,
        updated_at: '2026-10-31T00:01:00Z',
      },
    };
    const running = {
      ...listing,
      id: '44444444-4444-4444-4444-444444444444',
      requested_checks: ['adaptive_cruise_control'],
      latest_audit: {
        job_id: '55555555-5555-5555-5555-555555555555',
        status: 'running',
        current_stage: 'muse',
        acc_state: null,
        spend_usd: '0.0000',
        model_calls_made: 0,
        updated_at: '2026-10-31T00:01:00Z',
      },
    };
    const pending = {
      ...listing,
      id: '66666666-6666-6666-6666-666666666666',
      requested_checks: ['adaptive_cruise_control'],
      latest_audit: null,
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () => new Response(JSON.stringify({ items: [audited, running, pending], count: 3 })),
      ),
    );
    render(Dashboard, { props: { gateSetup: false } });
    expect(await screen.findByText('ACC: PASS')).toBeInTheDocument();
    expect(screen.getByText('Audit running…')).toBeInTheDocument();
    expect(screen.getByText('ACC requested — no audit yet')).toBeInTheDocument();
  });

  it('shows a completed audit with no ACC finding as unavailable', async () => {
    const unavailable = {
      ...listing,
      requested_checks: ['adaptive_cruise_control'],
      latest_audit: {
        job_id: '77777777-7777-7777-7777-777777777777',
        status: 'complete',
        current_stage: 'done',
        acc_state: null,
        spend_usd: '0.0100',
        model_calls_made: 2,
        updated_at: '2026-10-31T00:01:00Z',
      },
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ items: [unavailable], count: 1 }))),
    );
    render(Dashboard, { props: { gateSetup: false } });
    expect(await screen.findByText('ACC finding unavailable')).toBeInTheDocument();
    expect(screen.queryByText(/ACC: (PASS|FAIL|INCONCLUSIVE)/)).not.toBeInTheDocument();
  });

  it('never shows a failed audit as an ACC evidence result', async () => {
    const failed = {
      ...listing,
      requested_checks: ['adaptive_cruise_control'],
      latest_audit: {
        job_id: '33333333-3333-3333-3333-333333333333',
        status: 'failed',
        current_stage: 'muse',
        acc_state: null,
        spend_usd: '0.0000',
        model_calls_made: 1,
        updated_at: '2026-10-31T00:01:00Z',
      },
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ items: [failed], count: 1 }))),
    );
    render(Dashboard, { props: { gateSetup: false } });
    expect(await screen.findByText('Audit failed')).toBeInTheDocument();
    expect(screen.queryByText(/ACC: (PASS|FAIL|INCONCLUSIVE)/)).not.toBeInTheDocument();
  });
});
