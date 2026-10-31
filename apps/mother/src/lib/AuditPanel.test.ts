import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AuditPanel from './AuditPanel.svelte';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const LISTING = '11111111-1111-1111-1111-111111111111';
const JOB = '22222222-2222-2222-2222-222222222222';

const baseJob = {
  id: JOB,
  listing_id: LISTING,
  status: 'complete',
  current_stage: 'reconcile',
  requested_by: 'operator',
  prompt_version: 'm1',
  pricing_version: 'test',
  max_calls: 60,
  calls_made: 4,
  spend_cap_usd: '2.00',
  spend_usd: '0.0271',
  unknown_cost_calls: 0,
  worker_id: 'w-x',
  error_code: '',
  error_detail: '',
  observation_count: 2,
  crop_count: 1,
  finding_count: 2,
  execution_count: 6,
  created_at: '2026-10-31T00:00:00Z',
  updated_at: '2026-10-31T00:01:00Z',
};

const findings = [
  {
    id: 'f1',
    claim_key: 'c1',
    claim_text: 'adaptive cruise control',
    state: 'visually_supported',
    summary: 'Muse sighting confirmed by SAM localization',
    evidence: { rule: 'v2', observations: [] },
    created_at: '2026-10-31T00:01:00Z',
    updated_at: '2026-10-31T00:01:00Z',
  },
  {
    id: 'f2',
    claim_key: 'c2',
    claim_text: 'lane keeping assist',
    state: 'insufficient_evidence',
    summary: 'no confirmed sighting in 2 photo observation(s)',
    evidence: { rule: 'v2', observations: [] },
    created_at: '2026-10-31T00:01:00Z',
    updated_at: '2026-10-31T00:01:00Z',
  },
];

const evidence = {
  observations: [
    {
      id: 'o1',
      claim_key: 'adaptive cruise control',
      photo_id: '33333333-3333-3333-3333-333333333333',
      source: 'sam',
      verdict: 'present',
      boxes: [{ box: [8, 8, 32, 32], mask_ref: 'a'.repeat(32), confidence: 0.9 }],
      rationale: '1 segment(s)',
      extra: {},
      created_at: '2026-10-31T00:00:30Z',
    },
  ],
  crops: [
    {
      id: 'c1',
      source_photo_id: '33333333-3333-3333-3333-333333333333',
      box: [8, 8, 32, 32],
      width: 24,
      height: 24,
      has_mask: true,
    },
  ],
  executions: [
    {
      id: 'e1',
      stage: 'muse',
      provider: 'baseten',
      model_id: 'meta-models/muse-spark-open-1026',
      photo_ids: [],
      ok: true,
      latency_ms: 1200,
      usage: {},
      cost_usd: '0.0246',
      error_code: '',
      created_at: '2026-10-31T00:00:20Z',
    },
    {
      id: 'e2',
      stage: 'sam',
      provider: 'meta',
      model_id: 'sam-3.1',
      photo_ids: [],
      ok: true,
      latency_ms: 800,
      usage: {},
      cost_usd: null,
      error_code: '',
      created_at: '2026-10-31T00:00:25Z',
    },
  ],
};

function stubFetch(routes: Record<string, unknown>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url in routes) return new Response(JSON.stringify(routes[url]));
      throw new Error(`unexpected ${url}`);
    }),
  );
}

describe('AuditPanel', () => {
  it('offers to run the first audit when none exist', async () => {
    stubFetch({ [`/api/listings/${LISTING}/audits`]: [] });
    render(AuditPanel, { props: { listingId: LISTING } });
    expect(
      await screen.findByText(
        'No audits yet. Audits run Meta-only inference with server-side spend caps, and only when you start one.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run audit' })).toBeInTheDocument();
  });

  it('shows findings with approved state labels, never AI-verified', async () => {
    stubFetch({
      [`/api/listings/${LISTING}/audits`]: [baseJob],
      [`/api/audits/${JOB}/findings`]: findings,
      [`/api/audits/${JOB}/evidence`]: evidence,
    });
    render(AuditPanel, { props: { listingId: LISTING } });
    expect(await screen.findByText('Findings (2)')).toBeInTheDocument();
    expect(screen.getByText('Visually supported')).toBeInTheDocument();
    expect(screen.getByText('Insufficient evidence')).toBeInTheDocument();
    expect(screen.getAllByText('adaptive cruise control').length).toBeGreaterThan(0);
    expect(screen.queryByText(/AI-verified/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/AI verified/i)).not.toBeInTheDocument();
  });

  it('shows spend, observations, crops, ledger with unknown cost', async () => {
    stubFetch({
      [`/api/listings/${LISTING}/audits`]: [baseJob],
      [`/api/audits/${JOB}/findings`]: findings,
      [`/api/audits/${JOB}/evidence`]: evidence,
    });
    render(AuditPanel, { props: { listingId: LISTING } });
    expect(await screen.findByText(/\$0\.0271/)).toBeInTheDocument();
    expect(screen.getByText('Observations (1)')).toBeInTheDocument();
    expect(screen.getByText('Crops (1)')).toBeInTheDocument();
    expect(screen.getByText(/cost unknown/)).toBeInTheDocument();
    const mask = screen.getByAltText('Segmentation mask 1 for adaptive cruise control');
    expect(mask.getAttribute('src')).toBe(`/api/audits/${JOB}/masks/${'a'.repeat(32)}`);
    const crop = screen.getByAltText(/Zoomed crop 24 by 24/);
    expect(crop.getAttribute('src')).toBe(`/api/audits/${JOB}/crops/c1`);
  });

  it('shows the exact blocker for a paused job with fresh-audit action', async () => {
    const paused = {
      ...baseJob,
      status: 'paused',
      error_code: 'spend_cap',
      error_detail: 'spend $2.05 reached cap $2.00',
    };
    stubFetch({
      [`/api/listings/${LISTING}/audits`]: [paused],
      [`/api/audits/${JOB}/evidence`]: { observations: [], crops: [], executions: [] },
    });
    render(AuditPanel, { props: { listingId: LISTING } });
    expect(await screen.findByText('spend_cap')).toBeInTheDocument();
    expect(screen.getByText(/reached cap/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run fresh audit' })).toBeInTheDocument();
  });

  it('shows cancel for in-flight jobs', async () => {
    const running = { ...baseJob, status: 'running', current_stage: 'muse' };
    stubFetch({ [`/api/listings/${LISTING}/audits`]: [running] });
    render(AuditPanel, { props: { listingId: LISTING } });
    expect(await screen.findByText('running — muse')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });
});
