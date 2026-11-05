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
  calls_made: 6,
  model_calls_made: 4,
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
      source_crop_id: null,
      created_at: '2026-10-31T00:00:30Z',
    },
    {
      id: 'o2',
      claim_key: 'c1',
      photo_id: '33333333-3333-3333-3333-333333333333',
      source: 'recheck',
      verdict: 'present',
      boxes: [],
      rationale: 'crop confirms the button',
      extra: { crop_ref: 'deadbeef', changed: true },
      source_crop_id: 'c1',
      created_at: '2026-10-31T00:00:40Z',
    },
  ],
  crops: [
    {
      id: 'c1',
      source_photo_id: '33333333-3333-3333-3333-333333333333',
      source_claim_key: 'c1',
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
      provider: 'meta',
      model_id: 'muse-spark-1.3-contributor',
      photo_ids: [],
      ok: true,
      attempts: 1,
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
      attempts: 2,
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
    expect(screen.getByText('Observations (2)')).toBeInTheDocument();
    expect(screen.getByText('Crops (1)')).toBeInTheDocument();
    expect(screen.getByText(/claim c1/)).toBeInTheDocument();
    expect(screen.getByText(/crop c1/)).toBeInTheDocument();
    expect(screen.getByText(/changed/)).toBeInTheDocument();
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

  it('sends scope acc when running an ACC audit', async () => {
    const posted: { url: string; body: unknown }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown, init?: RequestInit) => {
        if (String(url).endsWith('/audits') && init?.method === 'POST') {
          posted.push({ url: String(url), body: JSON.parse(String(init?.body)) });
          return new Response(
            JSON.stringify({
              job: { ...baseJob, id: JOB, status: 'queued', current_stage: '' },
              created: true,
            }),
          );
        }
        if (String(url) === '/api/csrf') {
          return new Response(JSON.stringify({ csrf_token: 'test-csrf' }));
        }
        if (String(url).endsWith('/audits')) return new Response(JSON.stringify([]));
        if (String(url).includes(`/api/audits/${JOB}/evidence`)) {
          return new Response(JSON.stringify({ observations: [], crops: [], executions: [] }));
        }
        throw new Error(`unexpected fetch ${String(url)}`);
      }),
    );
    render(AuditPanel, {
      props: {
        listingId: LISTING,
        advertisedClaims: ['Adaptive Cruise Control', 'Bluetooth'],
        requestedChecks: [],
      },
    });
    (await screen.findByRole('button', { name: 'Run ACC Audit' })).click();
    await vi.waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0].body).toEqual({ scope: 'acc' });
  });

  it('shows cancel for in-flight jobs', async () => {
    const running = { ...baseJob, status: 'running', current_stage: 'muse' };
    stubFetch({ [`/api/listings/${LISTING}/audits`]: [running] });
    render(AuditPanel, { props: { listingId: LISTING } });
    expect(await screen.findByText('running — muse')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });

  it('shows photo-level stage detail and a paced ETA while observing', async () => {
    const running = {
      ...baseJob,
      status: 'running',
      current_stage: 'muse',
      calls_made: 10,
      model_calls_made: 9,
      created_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
    };
    const inFlightEvidence = {
      observations: [],
      crops: [],
      executions: [
        {
          id: 'e0',
          stage: 'screening',
          provider: 'local',
          model_id: '',
          photo_ids: [],
          ok: true,
          attempts: 1,
          latency_ms: 50,
          usage: { claims: 2, photos: 31, integrity: 'sha256-verified' },
          cost_usd: null,
          error_code: '',
          created_at: running.created_at,
        },
        {
          id: 'e1',
          stage: 'muse',
          provider: 'meta',
          model_id: 'muse-spark-1.3-contributor',
          photo_ids: [],
          ok: true,
          attempts: 1,
          latency_ms: 9000,
          usage: {
            prompt_version: 'triage-v1',
            triage_included: 6,
            triage_excluded: ['x1', 'x2', 'x3', 'x4'],
            filename_excluded: ['f0'],
          },
          cost_usd: '0.0001',
          error_code: '',
          created_at: running.created_at,
        },
        {
          id: 'e2',
          stage: 'muse',
          provider: 'meta',
          model_id: 'muse-spark-1.3-contributor',
          photo_ids: ['p1'],
          ok: true,
          attempts: 1,
          latency_ms: 12000,
          usage: { prompt_version: 'm2' },
          cost_usd: '0.0005',
          error_code: '',
          created_at: running.created_at,
        },
        {
          id: 'e3',
          stage: 'muse',
          provider: 'meta',
          model_id: 'muse-spark-1.3-contributor',
          photo_ids: ['p2'],
          ok: true,
          attempts: 1,
          latency_ms: 12000,
          usage: { prompt_version: 'm2' },
          cost_usd: '0.0005',
          error_code: '',
          created_at: running.created_at,
        },
      ],
    };
    stubFetch({
      [`/api/listings/${LISTING}/audits`]: [running],
      [`/api/audits/${JOB}`]: running,
      [`/api/audits/${JOB}/evidence`]: inFlightEvidence,
    });
    render(AuditPanel, { props: { listingId: LISTING } });
    expect(
      await screen.findByText('Examining photo 3 of 6 (4 by triage + 1 by filename)'),
    ).toBeInTheDocument();
    expect(screen.getByText(/~[0-9]+ (min|sec) left/)).toBeInTheDocument();
  });

  it('shows triage narrowing before photo observation starts', async () => {
    const running = {
      ...baseJob,
      status: 'running',
      current_stage: 'muse',
      calls_made: 1,
      model_calls_made: 0,
    };
    stubFetch({
      [`/api/listings/${LISTING}/audits`]: [running],
      [`/api/audits/${JOB}`]: running,
      [`/api/audits/${JOB}/evidence`]: {
        observations: [],
        crops: [],
        executions: [
          {
            id: 'e0',
            stage: 'screening',
            provider: 'local',
            model_id: '',
            photo_ids: [],
            ok: true,
            attempts: 1,
            latency_ms: 50,
            usage: { claims: 66, photos: 31 },
            cost_usd: null,
            error_code: '',
            created_at: running.created_at,
          },
        ],
      },
    });
    render(AuditPanel, { props: { listingId: LISTING } });
    expect(await screen.findByText(/Narrowing 31 photos to the relevant ones/)).toBeInTheDocument();
    expect(screen.getByText('estimating pace…')).toBeInTheDocument();
  });

  it('shows a PASS banner for seller-advertised ACC with visual support', async () => {
    stubFetch({
      [`/api/listings/${LISTING}/audits`]: [baseJob],
      [`/api/audits/${JOB}/findings`]: findings,
      [`/api/audits/${JOB}/evidence`]: evidence,
    });
    render(AuditPanel, {
      props: { listingId: LISTING, advertisedClaims: ['Adaptive Cruise Control'] },
    });
    expect(await screen.findByText(/PASS — Visual evidence supports ACC/)).toBeInTheDocument();
    expect(screen.getByText(/Seller-advertised ACC/)).toBeInTheDocument();
    // The ACC finding leads the list.
    const items = screen.getAllByText('adaptive cruise control');
    expect(items.length).toBeGreaterThan(0);
  });

  it('shows ACC finding unavailable when no ACC finding exists', async () => {
    const laneOnly = findings.filter((f) => f.claim_text !== 'adaptive cruise control');
    stubFetch({
      [`/api/listings/${LISTING}/audits`]: [baseJob],
      [`/api/audits/${JOB}/findings`]: laneOnly,
      [`/api/audits/${JOB}/evidence`]: evidence,
    });
    render(AuditPanel, {
      props: {
        listingId: LISTING,
        advertisedClaims: ['leather seats'],
        requestedChecks: ['adaptive_cruise_control'],
      },
    });
    // A completed audit with no ACC finding is unavailable, never an
    // evidence-based INCONCLUSIVE result.
    expect(await screen.findByText(/^ACC finding unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/Operator-requested check \(not a seller claim\)/)).toBeInTheDocument();
    expect(screen.queryByText(/INCONCLUSIVE — ACC cannot be established/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^FAIL/)).not.toBeInTheDocument();
  });

  it('shows a failed audit as a non-evidence outcome, never INCONCLUSIVE', async () => {
    const failed = {
      ...baseJob,
      status: 'failed',
      error_code: 'provider_error',
      error_detail: 'upstream 502',
    };
    stubFetch({
      [`/api/listings/${LISTING}/audits`]: [failed],
      [`/api/audits/${JOB}/evidence`]: { observations: [], crops: [], executions: [] },
    });
    render(AuditPanel, {
      props: { listingId: LISTING, requestedChecks: ['adaptive_cruise_control'] },
    });
    expect(await screen.findByText(/not an ACC evidence result/)).toBeInTheDocument();
    expect(screen.queryByText(/INCONCLUSIVE — ACC/)).not.toBeInTheDocument();
    expect(screen.queryByText(/PASS — Visual/)).not.toBeInTheDocument();
  });

  it('offers Run ACC Audit when the check is pending', async () => {
    stubFetch({ [`/api/listings/${LISTING}/audits`]: [] });
    render(AuditPanel, {
      props: { listingId: LISTING, requestedChecks: ['adaptive_cruise_control'] },
    });
    expect(await screen.findByRole('button', { name: 'Run ACC Audit' })).toBeInTheDocument();
  });
});
