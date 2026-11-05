<script lang="ts">
  import { ApiError, backend, type AuditEvidence, type AuditFinding, type AuditJob } from './api';
  import {
    assessAcc,
    isAccAdvertised,
    isAccRequested,
    isAuditInFlight,
    operationalAccMessage,
    type AccAssessment,
  } from './accVerdict';

  let {
    listingId,
    advertisedClaims = [],
    requestedChecks = [],
  }: { listingId: string; advertisedClaims?: string[]; requestedChecks?: string[] } = $props();

  let jobs = $state<AuditJob[]>([]);
  let findings = $state<AuditFinding[]>([]);
  let evidence = $state<AuditEvidence | null>(null);
  let loading = $state(true);
  let acting = $state(false);
  let error = $state('');
  let actionError = $state('');

  const latest = $derived(jobs.length > 0 ? jobs[0] : null);
  const inFlight = $derived(latest !== null && isAuditInFlight(latest.status));

  /** Whether ACC is in scope: seller-advertised, operator-requested, or both. */
  const wantsAcc = $derived(isAccAdvertised(advertisedClaims) || isAccRequested(requestedChecks));

  function isAccFinding(finding: AuditFinding): boolean {
    return (
      /user-requested check/i.test(finding.claim_text) || isAccAdvertised([finding.claim_text])
    );
  }

  const accFinding = $derived(findings.find(isAccFinding) ?? null);
  const orderedFindings = $derived(
    accFinding ? [accFinding, ...findings.filter((f) => f.id !== accFinding.id)] : findings,
  );
  const orderedObservations = $derived.by(() => {
    if (!evidence || !accFinding) return (evidence?.observations ?? []).slice();
    const keys = new Set([accFinding.claim_key, accFinding.claim_text]);
    const accObs = evidence.observations.filter(
      (o) => keys.has(o.claim_key) || /user-requested check/i.test(o.claim_key),
    );
    const rest = evidence.observations.filter((o) => !accObs.includes(o));
    return [...accObs, ...rest];
  });

  const accAssessment = $derived.by((): AccAssessment | null => {
    if (!wantsAcc || !latest || latest.status !== 'complete') return null;
    return assessAcc({
      advertisedClaims,
      requestedChecks,
      accState: accFinding?.state ?? null,
    });
  });

  const operationalMessage = $derived(latest ? operationalAccMessage(latest.status) : null);

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
        return 'border-[#e6c795] bg-[#fdf0dc] text-[#3a2f23]';
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
      try {
        // Ledger snapshot for in-flight progress (stage, triage outcome,
        // photos covered). Failures keep the last known state; the poll
        // tick retries.
        evidence = await backend.getEvidence(job.id);
      } catch {
        // Keep the last known ledger; the next tick retries.
      }
    }
  }

  function usageNumber(exec: { usage: Record<string, unknown> } | undefined, key: string) {
    const v = exec?.usage[key];
    return typeof v === 'number' && Number.isFinite(v) ? v : null;
  }

  function formatEta(ms: number): string {
    const sec = Math.max(1, Math.round(ms / 1000));
    return sec < 90 ? `~${sec} sec left` : `~${Math.round(sec / 60)} min left`;
  }

  /**
   * In-flight progress from the job row + execution ledger. The photo total
   * comes from screening, the kept/skipped split from the triage-v1 row, and
   * photos-covered from distinct muse photo ids. The ETA extrapolates the
   * observed per-call pace over planned remaining calls (triage counts as
   * its own call); it is an estimate, always prefixed with ~, and bounded by
   * the call budget.
   */
  const flightProgress = $derived.by(() => {
    if (!latest || !inFlight) return null;
    const execs = evidence?.executions ?? [];
    const screening = execs.find((e) => e.stage === 'screening');
    const totalPhotos = usageNumber(screening, 'photos');
    const triage = execs.find((e) => e.usage?.prompt_version === 'triage-v1' && e.ok);
    const kept = usageNumber(triage, 'triage_included') ?? totalPhotos;
    const skipped = Array.isArray(triage?.usage?.triage_excluded)
      ? (triage?.usage?.triage_excluded as unknown[]).length
      : 0;
    const filenameSkipped = Array.isArray(triage?.usage?.filename_excluded)
      ? (triage?.usage?.filename_excluded as unknown[]).length
      : 0;
    const museCalls = execs.filter(
      (e) => e.stage === 'muse' && e.ok && e.usage?.prompt_version !== 'triage-v1',
    );
    const donePhotos = new Set(museCalls.flatMap((e) => e.photo_ids)).size;
    const samDone = execs.filter((e) => e.stage === 'sam' && e.ok).length;
    const rechecksDone = execs.filter((e) => e.stage === 'recheck' && e.ok).length;

    let stage: string;
    let remainingCalls: number | null = null;
    switch (latest.current_stage) {
      case 'screening':
        stage =
          totalPhotos === null
            ? 'Starting — verifying evidence integrity…'
            : `Verifying ${totalPhotos} photos…`;
        break;
      case 'muse':
        if (!triage) {
          stage =
            totalPhotos === null
              ? 'Narrowing photos to the relevant ones…'
              : `Narrowing ${totalPhotos} photos to the relevant ones (1 cheap call)…`;
        } else {
          const total = kept ?? donePhotos;
          const skipParts: string[] = [];
          if (skipped > 0) skipParts.push(`${skipped} by triage`);
          if (filenameSkipped > 0) skipParts.push(`${filenameSkipped} by filename`);
          const skippedNote = skipParts.length > 0 ? ` (${skipParts.join(' + ')})` : '';
          stage = `Examining photo ${Math.min(donePhotos + 1, Math.max(total, 1))} of ${total}${skippedNote}`;
          const perPhoto = donePhotos > 0 ? museCalls.length / donePhotos : 1;
          remainingCalls = Math.max(0, total - donePhotos) * perPhoto + 6;
        }
        break;
      case 'sam':
        stage = `Localizing observed controls (${samDone} pair${samDone === 1 ? '' : 's'} so far)…`;
        remainingCalls = 6;
        break;
      case 'crop':
        stage = `Cutting evidence crops (${latest.crop_count} so far)…`;
        remainingCalls = 4;
        break;
      case 'recheck':
        stage = `Re-examining ambiguous crops (${rechecksDone} so far)…`;
        remainingCalls = 3;
        break;
      case 'reconcile':
        stage = 'Assigning findings…';
        remainingCalls = 0;
        break;
      default:
        stage = latest.status === 'queued' ? 'Queued — waiting for the worker…' : 'Working…';
        break;
    }

    let eta = 'estimating pace…';
    const elapsed = Date.parse(latest.created_at);
    const pace =
      Number.isFinite(elapsed) && latest.model_calls_made >= 2
        ? (Date.now() - elapsed) / latest.model_calls_made
        : null;
    if (pace !== null && remainingCalls !== null) {
      const budgetLeft = Math.max(0, latest.max_calls - latest.calls_made);
      const planned = Math.min(remainingCalls, budgetLeft);
      eta =
        remainingCalls > budgetLeft
          ? `up to ${formatEta(pace * planned)} (call budget)`
          : formatEta(pace * planned);
    }
    return { stage, eta };
  });

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
      // The ACC button audits only ACC-matching claims. The general button
      // audits every claim on the listing.
      const { job } = await backend.queueAudit(listingId, wantsAcc ? 'acc' : undefined);
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
    <p class="mt-2 text-sm text-[#8a7660]">Loading audits…</p>
  {:else if error}
    <div
      class="mt-2 rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-800"
      role="alert"
    >
      {error}
    </div>
  {:else if !latest}
    {#if wantsAcc}
      <p class="mt-2 text-sm text-slate-700">
        ACC check pending — {isAccRequested(requestedChecks) && !isAccAdvertised(advertisedClaims)
          ? 'operator-requested (not a seller claim)'
          : 'seller-advertised'}. Audits run Meta-only inference with server-side spend caps, and
        only when you start one.
      </p>
    {:else}
      <p class="mt-2 text-sm text-[#8a7660]">
        No audits yet. Audits run Meta-only inference with server-side spend caps, and only when you
        start one.
      </p>
    {/if}
    <button
      class="mt-2 rounded-md bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
      disabled={acting}
      onclick={runAudit}
    >
      {acting ? 'Starting…' : wantsAcc ? 'Run ACC Audit' : 'Run audit'}
    </button>
    {#if actionError}
      <div
        class="mt-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800"
        role="alert"
      >
        {actionError}
      </div>
    {/if}
  {:else}
    <div class="mt-2 rounded-md border border-[#e6c795] bg-[#fff8ef] p-4 text-sm">
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-medium">Audit {shortId(latest.id)}</span>
        <span class="rounded-full bg-[#f3d3a2] px-3 py-1 text-[#3a2f23]">
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
            {acting ? 'Starting…' : wantsAcc ? 'Run ACC Audit' : 'Run audit'}
          </button>
        {/if}
      </div>
      {#if inFlight && flightProgress}
        <p class="mt-2 font-medium">{flightProgress.stage}</p>
        <p class="text-[#6f5f4c]">{flightProgress.eta}</p>
      {/if}
      <div class="mt-2 text-[#6f5f4c]">
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
        <p class="mt-2 text-[#8a7660]">Cancelled before completion. No findings.</p>
      {/if}
    </div>

    {#if operationalMessage}
      <div
        class="mt-2 rounded-md border border-[#e6c795] bg-[#fdf0dc] p-3 text-sm text-[#3a2f23]"
        role="status"
      >
        {operationalMessage}
      </div>
    {/if}

    {#if accAssessment}
      {@const assessment = accAssessment}
      <div
        class="mt-2 rounded-md border p-3 text-sm"
        class:border-emerald-200={assessment.available && assessment.label === 'PASS'}
        class:bg-emerald-50={assessment.available && assessment.label === 'PASS'}
        class:text-emerald-900={assessment.available && assessment.label === 'PASS'}
        class:border-red-200={assessment.available && assessment.label === 'FAIL'}
        class:bg-red-50={assessment.available && assessment.label === 'FAIL'}
        class:text-red-900={assessment.available && assessment.label === 'FAIL'}
        class:border-amber-200={assessment.available && assessment.label === 'INCONCLUSIVE'}
        class:bg-amber-50={assessment.available && assessment.label === 'INCONCLUSIVE'}
        class:text-amber-900={assessment.available && assessment.label === 'INCONCLUSIVE'}
        class:border-[#e6c795]={!assessment.available}
        class:bg-[#fdf0dc]={!assessment.available}
        class:text-[#3a2f23]={!assessment.available}
        role="status"
      >
        <div class="font-semibold">{assessment.headline}</div>
        <div class="mt-1">
          Underlying finding: {assessment.state ?? 'none'} ·
          {assessment.provenance === 'seller-advertised'
            ? 'Seller-advertised ACC'
            : assessment.provenance === 'operator-requested'
              ? 'Operator-requested check (not a seller claim)'
              : assessment.provenance === 'both'
                ? 'Seller-advertised and operator-requested'
                : 'No ACC claim recorded'}
        </div>
        <div class="mt-1">{assessment.limitation}</div>
        {#if !assessment.available || assessment.label === 'INCONCLUSIVE'}
          <div class="mt-1">Full evidence is below — a badge alone is never the whole result.</div>
        {/if}
      </div>
    {/if}

    {#if actionError}
      <div
        class="mt-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800"
        role="alert"
      >
        {actionError}
      </div>
    {/if}

    {#if orderedFindings.length > 0}
      <h3 class="mt-4 text-base font-semibold">Findings ({orderedFindings.length})</h3>
      <ul class="mt-2 space-y-2 text-sm">
        {#each orderedFindings as finding (finding.id)}
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
      {#if orderedObservations.length > 0}
        <h4 class="mt-2 text-sm font-semibold">
          Observations ({orderedObservations.length})
        </h4>
        <ul class="mt-1 space-y-2 text-sm">
          {#each orderedObservations as obs (obs.id)}
            <li class="rounded-md border border-[#e6c795] bg-[#fff8ef] p-3">
              <div>
                <span class="font-medium">{obs.claim_key}</span>
                <span class="text-[#8a7660]">
                  · {obs.source} · {obs.verdict} · photo {shortId(
                    obs.photo_id,
                  )}{#if obs.source === 'recheck' && obs.source_crop_id}
                    · crop {shortId(obs.source_crop_id)}{#if obs.extra?.changed === true}
                      · changed{/if}{/if}</span
                >
              </div>
              {#if obs.rationale}
                <div class="mt-1 text-[#6f5f4c]">{obs.rationale}</div>
              {/if}
              {#each obs.boxes as entry, i (i)}
                {#if entry.mask_ref}
                  <img
                    src={backend.maskUrl(latest.id, entry.mask_ref)}
                    alt={`Segmentation mask ${i + 1} for ${obs.claim_key}`}
                    class="mt-2 h-24 border border-[#e6c795] bg-slate-950"
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
            <li class="overflow-hidden rounded-md border border-[#e6c795] bg-[#fff8ef]">
              <img
                src={backend.cropUrl(latest.id, crop.id)}
                alt={`Zoomed crop ${crop.width} by ${crop.height} from photo ${shortId(crop.source_photo_id)}`}
                class="aspect-square w-full object-contain bg-slate-950"
                loading="lazy"
              />
              <div class="p-2 text-xs text-[#6f5f4c]">
                {crop.width}×{crop.height} · photo {shortId(
                  crop.source_photo_id,
                )}{#if crop.source_claim_key}
                  · claim {crop.source_claim_key}{/if}
              </div>
            </li>
          {/each}
        </ul>
      {/if}
      {#if evidence.executions.length > 0}
        <h4 class="mt-2 text-sm font-semibold">Ledger ({evidence.executions.length})</h4>
        <ul class="mt-1 space-y-1 font-mono text-xs">
          {#each evidence.executions as exec (exec.id)}
            <li class="rounded border border-[#e6c795] bg-[#fff8ef] p-2">
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
      <ul class="mt-1 space-y-1 text-sm text-[#6f5f4c]">
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
