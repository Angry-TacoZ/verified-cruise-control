<script lang="ts">
  import { ApiError, backend, type AuditEvidence, type AuditFinding, type AuditJob } from './api';

  let { listingId }: { listingId: string } = $props();

  let jobs = $state<AuditJob[]>([]);
  let findings = $state<AuditFinding[]>([]);
  let evidence = $state<AuditEvidence | null>(null);
  let loading = $state(true);
  let acting = $state(false);
  let error = $state('');
  let actionError = $state('');

  const latest = $derived(jobs.length > 0 ? jobs[0] : null);
  const inFlight = $derived(
    latest !== null && (latest.status === 'queued' || latest.status === 'running'),
  );

  function stateLabel(state: string): string {
    switch (state) {
      case 'verified':
        return 'Verified';
      case 'visually_supported':
        return 'Visually supported';
      case 'contradicted':
        return 'Contradicted';
      case 'insufficient_evidence':
        return 'Insufficient evidence';
      case 'not_visually_verifiable':
        return 'Not visually verifiable';
      default:
        return state;
    }
  }

  function stateTone(state: string): string {
    switch (state) {
      case 'verified':
      case 'visually_supported':
        return 'border-emerald-200 bg-emerald-50 text-emerald-900';
      case 'contradicted':
        return 'border-red-200 bg-red-50 text-red-900';
      default:
        return 'border-slate-200 bg-slate-50 text-slate-800';
    }
  }

  function money(value: string): string {
    const num = Number(value);
    return Number.isFinite(num) ? `$${num.toFixed(4)}` : value;
  }

  function shortId(id: string): string {
    return id.slice(0, 8);
  }

  async function loadDetail(job: AuditJob) {
    if (job.status === 'complete') {
      const [found, ev] = await Promise.all([
        backend.listFindings(job.id),
        backend.getEvidence(job.id),
      ]);
      findings = found;
      evidence = ev;
    } else if (job.status === 'failed' || job.status === 'paused') {
      evidence = await backend.getEvidence(job.id);
      findings = [];
    } else {
      findings = [];
      evidence = null;
    }
  }

  async function refresh() {
    try {
      jobs = await backend.listAudits(listingId);
      if (jobs.length > 0) await loadDetail(jobs[0]);
    } catch (err) {
      error = err instanceof Error ? err.message : 'Failed to load audits.';
    }
  }

  async function runAudit() {
    acting = true;
    actionError = '';
    try {
      const { job } = await backend.queueAudit(listingId);
      jobs = [job, ...jobs.filter((j) => j.id !== job.id)];
      await loadDetail(job);
    } catch (err) {
      actionError =
        err instanceof ApiError && err.status === 403
          ? `Cannot start audit: ${err.message}`
          : err instanceof Error
            ? err.message
            : 'Failed to start audit.';
    } finally {
      acting = false;
    }
  }

  async function cancelAudit() {
    if (!latest) return;
    acting = true;
    actionError = '';
    try {
      const job = await backend.cancelAudit(latest.id);
      jobs = [job, ...jobs.slice(1)];
      await loadDetail(job);
    } catch (err) {
      actionError = err instanceof Error ? err.message : 'Failed to cancel audit.';
    } finally {
      acting = false;
    }
  }

  async function retryAudit() {
    if (!latest) return;
    acting = true;
    actionError = '';
    try {
      const { job } = await backend.retryAudit(latest.id);
      jobs = [job, ...jobs];
      await loadDetail(job);
    } catch (err) {
      actionError = err instanceof Error ? err.message : 'Failed to retry audit.';
    } finally {
      acting = false;
    }
  }

  $effect(() => {
    jobs = [];
    findings = [];
    evidence = null;
    loading = true;
    error = '';
    void refresh().finally(() => {
      loading = false;
    });
  });

  // Poll the latest job while it is in flight; stop on settle.
  $effect(() => {
    if (!inFlight || !latest) return;
    const id = latest.id;
    const timer = setInterval(() => {
      void (async () => {
        try {
          const job = await backend.getAudit(id);
          jobs = [job, ...jobs.filter((j) => j.id !== job.id)];
          if (job.status !== 'queued' && job.status !== 'running') {
            await loadDetail(job);
          }
        } catch {
          // Keep the last known state; the next tick retries.
        }
      })();
    }, 2000);
    return () => clearInterval(timer);
  });
</script>

<section aria-labelledby="audits">
  <h2 id="audits" class="mt-6 text-lg font-semibold">Audits</h2>
  {#if loading}
    <p class="mt-2 text-sm text-slate-500">Loading audits…</p>
  {:else if error}
    <div
      class="mt-2 rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-800"
      role="alert"
    >
      {error}
    </div>
  {:else if !latest}
    <p class="mt-2 text-sm text-slate-500">
      No audits yet. Audits run Meta-only inference with server-side spend caps, and only when you
      start one.
    </p>
    <button
      class="mt-2 rounded-md bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
      disabled={acting}
      onclick={runAudit}
    >
      {acting ? 'Starting…' : 'Run audit'}
    </button>
  {:else}
    <div class="mt-2 rounded-md border border-slate-200 bg-white p-4 text-sm">
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-medium">Audit {shortId(latest.id)}</span>
        <span class="rounded-full bg-slate-100 px-3 py-1 text-slate-800">
          {latest.status}{inFlight ? ` — ${latest.current_stage}` : ''}
        </span>
        {#if inFlight}
          <button
            class="ml-auto rounded-md border border-slate-300 px-3 py-1 disabled:opacity-50"
            disabled={acting}
            onclick={cancelAudit}
          >
            Cancel
          </button>
        {:else if latest.status === 'failed' || latest.status === 'paused'}
          <button
            class="ml-auto rounded-md bg-slate-900 px-3 py-1 text-white disabled:opacity-50"
            disabled={acting}
            onclick={retryAudit}
          >
            {acting ? 'Starting…' : 'Run fresh audit'}
          </button>
        {:else}
          <button
            class="ml-auto rounded-md border border-slate-300 px-3 py-1 disabled:opacity-50"
            disabled={acting}
            onclick={runAudit}
          >
            {acting ? 'Starting…' : 'Run audit'}
          </button>
        {/if}
      </div>
      <div class="mt-2 text-slate-600">
        {latest.model_calls_made}/{latest.max_calls} model calls · {money(latest.spend_usd)} of
        {money(latest.spend_cap_usd)} cap{#if latest.unknown_cost_calls > 0}
          · {latest.unknown_cost_calls} unknown-cost call(s){/if}
        · {latest.observation_count} observations · {latest.crop_count} crops
      </div>
      {#if latest.status === 'paused' || latest.status === 'failed'}
        <div
          class="mt-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-amber-900"
          role="alert"
        >
          <span class="font-medium">{latest.error_code || latest.status}</span>
          {#if latest.error_detail}
            <span> — {latest.error_detail}</span>
          {/if}
        </div>
      {/if}
      {#if latest.status === 'cancelled'}
        <p class="mt-2 text-slate-500">Cancelled before completion. No findings.</p>
      {/if}
    </div>

    {#if actionError}
      <div
        class="mt-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800"
        role="alert"
      >
        {actionError}
      </div>
    {/if}

    {#if findings.length > 0}
      <h3 class="mt-4 text-base font-semibold">Findings ({findings.length})</h3>
      <ul class="mt-2 space-y-2 text-sm">
        {#each findings as finding (finding.id)}
          <li class={`rounded-md border p-3 ${stateTone(finding.state)}`}>
            <div class="font-medium">{finding.claim_text || finding.claim_key}</div>
            <div class="mt-1">
              <span class="font-semibold">{stateLabel(finding.state)}</span>
              {#if finding.summary}
                <span> — {finding.summary}</span>
              {/if}
            </div>
          </li>
        {/each}
      </ul>
    {/if}

    {#if evidence && (evidence.observations.length > 0 || evidence.crops.length > 0 || evidence.executions.length > 0)}
      <h3 class="mt-4 text-base font-semibold">Evidence</h3>
      {#if evidence.observations.length > 0}
        <h4 class="mt-2 text-sm font-semibold">
          Observations ({evidence.observations.length})
        </h4>
        <ul class="mt-1 space-y-2 text-sm">
          {#each evidence.observations as obs (obs.id)}
            <li class="rounded-md border border-slate-200 bg-white p-3">
              <div>
                <span class="font-medium">{obs.claim_key}</span>
                <span class="text-slate-500">
                  · {obs.source} · {obs.verdict} · photo {shortId(obs.photo_id)}</span
                >
              </div>
              {#if obs.rationale}
                <div class="mt-1 text-slate-600">{obs.rationale}</div>
              {/if}
              {#each obs.boxes as entry, i (i)}
                {#if entry.mask_ref}
                  <img
                    src={backend.maskUrl(latest.id, entry.mask_ref)}
                    alt={`Segmentation mask ${i + 1} for ${obs.claim_key}`}
                    class="mt-2 h-24 border border-slate-200 bg-slate-950"
                    loading="lazy"
                  />
                {/if}
              {/each}
            </li>
          {/each}
        </ul>
      {/if}
      {#if evidence.crops.length > 0}
        <h4 class="mt-2 text-sm font-semibold">Crops ({evidence.crops.length})</h4>
        <ul class="mt-1 grid grid-cols-2 gap-4 md:grid-cols-3">
          {#each evidence.crops as crop (crop.id)}
            <li class="overflow-hidden rounded-md border border-slate-200 bg-white">
              <img
                src={backend.cropUrl(latest.id, crop.id)}
                alt={`Zoomed crop ${crop.width} by ${crop.height} from photo ${shortId(crop.source_photo_id)}`}
                class="aspect-square w-full object-contain bg-slate-950"
                loading="lazy"
              />
              <div class="p-2 text-xs text-slate-600">
                {crop.width}×{crop.height} · photo {shortId(crop.source_photo_id)}
              </div>
            </li>
          {/each}
        </ul>
      {/if}
      {#if evidence.executions.length > 0}
        <h4 class="mt-2 text-sm font-semibold">Ledger ({evidence.executions.length})</h4>
        <ul class="mt-1 space-y-1 font-mono text-xs">
          {#each evidence.executions as exec (exec.id)}
            <li class="rounded border border-slate-200 bg-white p-2">
              {exec.stage} · {exec.provider}{#if exec.model_id}/{exec.model_id}{/if}
              · {exec.ok ? 'ok' : `failed (${exec.error_code})`} ·
              {exec.attempts} attempt{exec.attempts === 1 ? '' : 's'} ·
              {exec.cost_usd === null ? 'cost unknown' : money(exec.cost_usd)}
            </li>
          {/each}
        </ul>
      {/if}
    {/if}

    {#if jobs.length > 1}
      <h3 class="mt-4 text-base font-semibold">History ({jobs.length})</h3>
      <ul class="mt-1 space-y-1 text-sm text-slate-600">
        {#each jobs.slice(1) as job (job.id)}
          <li>
            {shortId(job.id)} · {job.status} · {money(job.spend_usd)} ·
            {new Date(job.created_at).toLocaleString()}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>
