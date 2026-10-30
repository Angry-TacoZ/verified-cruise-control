<script lang="ts">
  import { onMount } from 'svelte';
  import { browser } from 'wxt/browser';
  import { checkHealth } from '../../src/lib/api';
  import { extractorForUrl } from '../../src/lib/extractors';
  import type { ImportProgress } from '../../src/lib/uploader';
  import { getPairingToken, setPairingToken, sessionProgressStore } from '../../src/lib/store';

  const MOTHER_APP = import.meta.env.VITE_VCC_MOTHER_URL ?? 'http://127.0.0.1:5173';

  let backendOk = $state<boolean | null>(null);
  let paired = $state<boolean | null>(null);
  let tokenInput = $state('');
  let tabId = $state<number | null>(null);
  let tabUrl = $state('');
  let supported = $state(false);
  let busy = $state(false);
  let error = $state('');
  let progress = $state<ImportProgress | null>(null);
  // Whether the background worker currently has an import operation in
  // flight. Null means unknown (e.g. worker unreachable). An `active`
  // checkpoint with no running operation is a stalled import the operator
  // can resume or abandon.
  let operationRunning = $state<boolean | null>(null);
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  async function refreshTab() {
    try {
      const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
      tabId = tab?.id ?? null;
      tabUrl = tab?.url ?? '';
      supported = tabUrl !== '' && extractorForUrl(tabUrl) !== null;
    } catch {
      supported = false;
    }
  }

  async function refreshAll() {
    backendOk = await checkHealth();
    try {
      paired = (await getPairingToken()) !== null;
    } catch (err) {
      // Fail-closed storage restriction: without trusted-context storage the
      // popup cannot pair, so it shows the browser error instead of a form.
      paired = null;
      error = err instanceof Error ? err.message : 'Secure token storage is unavailable.';
      return;
    }
    progress = await sessionProgressStore().load();
    try {
      const state = (await browser.runtime.sendMessage({ type: 'vcc:get-state' })) as {
        operationRunning?: unknown;
      };
      operationRunning =
        typeof state?.operationRunning === 'boolean' ? state.operationRunning : null;
    } catch {
      operationRunning = null;
    }
    await refreshTab();
  }

  async function saveToken(event: SubmitEvent) {
    event.preventDefault();
    error = '';
    const value = tokenInput.trim();
    if (!value) {
      error = 'Paste the pairing token from the mother app.';
      return;
    }
    try {
      await setPairingToken(value);
    } catch (err) {
      error = err instanceof Error ? err.message : 'Secure token storage is unavailable.';
      return;
    }
    tokenInput = '';
    paired = true;
  }

  async function unpair() {
    try {
      await setPairingToken(null);
    } catch (err) {
      error = err instanceof Error ? err.message : 'Secure token storage is unavailable.';
      return;
    }
    await sessionProgressStore().clear();
    paired = false;
    progress = null;
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => void refreshAll(), 800);
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  async function runRemote(
    message: { type: 'vcc:start-import'; tabId: number } | { type: 'vcc:retry-failed' },
    failureLabel: string,
  ) {
    error = '';
    busy = true;
    startPolling();
    try {
      const res = (await browser.runtime.sendMessage(message)) as {
        ok: boolean;
        error?: string;
        progress?: ImportProgress;
      };
      if (!res?.ok) error = res?.error ?? failureLabel;
      else if (res.progress) progress = res.progress;
    } catch (err) {
      error = err instanceof Error ? err.message : failureLabel;
    } finally {
      busy = false;
      await refreshAll();
      if (!progress || progress.status !== 'active') stopPolling();
    }
  }

  async function startImport() {
    if (tabId === null) {
      error = 'No active tab.';
      return;
    }
    await runRemote({ type: 'vcc:start-import', tabId }, 'Import failed.');
  }

  async function retryFailed() {
    await runRemote({ type: 'vcc:retry-failed' }, 'Retry failed.');
  }

  async function cancelImport() {
    error = '';
    try {
      const res = (await browser.runtime.sendMessage({ type: 'vcc:cancel-import' })) as {
        ok: boolean;
        error?: string;
        progress?: ImportProgress;
      };
      // Idle cancel may abandon the checkpoint and return its terminal
      // state; a live cancel returns ok without progress. Either way,
      // surface a real failure instead of silently refreshing.
      if (!res?.ok) error = res?.error ?? 'Cancel failed.';
      else if (res.progress) progress = res.progress;
    } catch (err) {
      error = err instanceof Error ? err.message : 'Cancel failed.';
    } finally {
      await refreshAll();
    }
  }

  async function abandonImport() {
    error = '';
    busy = true;
    try {
      const res = (await browser.runtime.sendMessage({ type: 'vcc:abandon-import' })) as {
        ok: boolean;
        error?: string;
        progress?: ImportProgress;
      };
      if (!res?.ok) error = res?.error ?? 'Abandon failed.';
      else if (res.progress) progress = res.progress;
    } catch (err) {
      error = err instanceof Error ? err.message : 'Abandon failed.';
    } finally {
      busy = false;
      await refreshAll();
    }
  }

  function counts(): { done: number; total: number; failed: number } {
    const photos = progress?.photos ?? [];
    return {
      done: photos.filter((p) => p.status === 'done' || p.status === 'duplicate').length,
      total: photos.length,
      failed: photos.filter((p) => p.status === 'failed').length,
    };
  }

  onMount(() => {
    void refreshAll();
    return () => stopPolling();
  });
</script>

<div class="w-80 bg-white p-4 text-sm text-slate-900">
  <header class="flex items-center gap-2">
    <span
      class="flex h-7 w-7 items-center justify-center rounded-md bg-blue-600 text-xs font-bold text-white"
      >VC</span
    >
    <h1 class="font-semibold">Verified Cruise Control</h1>
    <span class="ml-auto" role="status" aria-label="Backend connection">
      {#if backendOk === null}
        <span class="text-slate-400">…</span>
      {:else if backendOk}
        <span class="rounded-full bg-emerald-100 px-2 py-0.5 text-xs text-emerald-800"
          >connected</span
        >
      {:else}
        <span class="rounded-full bg-red-100 px-2 py-0.5 text-xs text-red-800">offline</span>
      {/if}
    </span>
  </header>

  {#if backendOk === false}
    <p class="mt-3 rounded-md bg-red-50 p-2 text-xs text-red-800">
      Mother app backend unreachable. Start Django on 127.0.0.1:8000 first.
    </p>
  {/if}

  {#if paired === false}
    <form class="mt-3 space-y-2" onsubmit={saveToken}>
      <label class="block text-xs font-medium" for="pairing-token"
        >Pairing token (from the mother app)</label
      >
      <input
        id="pairing-token"
        class="w-full rounded-md border border-slate-300 px-2 py-1 font-mono text-xs"
        type="password"
        autocomplete="off"
        bind:value={tokenInput}
      />
      <button class="w-full rounded-md bg-slate-900 px-3 py-1.5 text-white" type="submit"
        >Pair extension</button
      >
    </form>
  {:else if paired === true}
    <div class="mt-3 flex items-center gap-2 text-xs">
      <span class="rounded-full bg-emerald-100 px-2 py-0.5 text-emerald-800">paired</span>
      <button class="ml-auto underline" type="button" onclick={() => void unpair()}>Unpair</button>
    </div>

    <div class="mt-3 rounded-md border border-slate-200 p-2 text-xs">
      {#if tabId === null}
        <p class="text-slate-500">No active tab.</p>
      {:else if supported}
        <p class="break-all"><span class="font-medium">Listing detected:</span> {tabUrl}</p>
      {:else}
        <p class="text-slate-500">Current tab is not a supported listing page.</p>
      {/if}
    </div>

    <button
      class="mt-3 w-full rounded-md bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50"
      type="button"
      disabled={busy || !supported}
      onclick={() => void startImport()}
    >
      {busy ? 'Importing…' : 'Import this listing'}
    </button>

    {#if progress && progress.status === 'active'}
      {@const c = counts()}
      {#if operationRunning === false}
        <div class="mt-2 rounded-md bg-amber-50 p-2 text-xs" role="status">
          Import interrupted — no upload is running. Stored photos and the listing are kept.
          <div class="mt-2 flex gap-2">
            <button
              class="rounded-md bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50"
              type="button"
              disabled={busy || !supported}
              onclick={() => void startImport()}
            >
              Resume import
            </button>
            <button
              class="rounded-md border border-slate-300 px-3 py-1.5 disabled:opacity-50"
              type="button"
              disabled={busy}
              onclick={() => void abandonImport()}
            >
              Abandon import
            </button>
          </div>
          {#if !supported}
            <p class="mt-1 text-slate-500">
              Reopen the listing page to resume; abandoning works without it.
            </p>
          {/if}
        </div>
      {:else}
        <div class="mt-2 text-xs" role="status">
          Uploading photos: {c.done}/{c.total}{c.failed > 0 ? ` (${c.failed} failed)` : ''}
          <button class="ml-2 underline" type="button" onclick={() => void cancelImport()}
            >Cancel</button
          >
        </div>
      {/if}
    {/if}
    {#if progress && progress.status !== 'active'}
      {@const c = counts()}
      <div class="mt-2 rounded-md bg-slate-100 p-2 text-xs" role="status">
        {#if progress.status === 'complete'}
          Import complete: {c.done}/{c.total} photos.
        {:else if progress.status === 'partial'}
          Import partially complete: {c.done}/{c.total} photos stored, {c.failed} failed.
          <button
            class="mt-2 w-full rounded-md bg-amber-600 px-3 py-1.5 text-white disabled:opacity-50"
            type="button"
            disabled={busy}
            onclick={() => void retryFailed()}
          >
            {busy ? 'Retrying…' : `Retry ${c.failed} failed photo${c.failed === 1 ? '' : 's'}`}
          </button>
        {:else}
          Import failed: {progress.error ?? 'no photos stored.'}
          {#if c.failed > 0}
            <button
              class="mt-2 w-full rounded-md bg-amber-600 px-3 py-1.5 text-white disabled:opacity-50"
              type="button"
              disabled={busy}
              onclick={() => void retryFailed()}
            >
              {busy ? 'Retrying…' : `Retry ${c.failed} failed photo${c.failed === 1 ? '' : 's'}`}
            </button>
          {/if}
        {/if}
        {#if progress.listingId}
          <a
            class="mt-1 block text-blue-700 underline"
            href={`${MOTHER_APP}/#/listings/${progress.listingId}`}
            target="_blank"
            rel="noreferrer"
          >
            Open in mother app →
          </a>
        {/if}
      </div>
    {/if}
  {/if}

  {#if error}
    <p class="mt-2 rounded-md bg-red-50 p-2 text-xs text-red-800" role="alert">{error}</p>
  {/if}
</div>
