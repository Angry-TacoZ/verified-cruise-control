<script lang="ts">
  import { onMount } from 'svelte';
  import { browser } from 'wxt/browser';
  import { checkHealth } from '../../src/lib/api';
  import {
    MAX_USER_FILES,
    isExtractedListing,
    type ExtractedListing,
    type HostAccessResponse,
    type ManualEntry,
    type PreviewResponse,
    type RequestedCheck,
    type UserSuppliedFile,
  } from '../../src/lib/protocol';
  import type { ImportProgress } from '../../src/lib/uploader';
  import { getPairingToken, setPairingToken, sessionProgressStore } from '../../src/lib/store';

  const MOTHER_APP = import.meta.env.VITE_VCC_MOTHER_URL ?? 'http://127.0.0.1:5173';

  let backendOk = $state<boolean | null>(null);
  let paired = $state<boolean | null>(null);
  let tokenInput = $state('');
  let tabId = $state<number | null>(null);
  let tabUrl = $state('');
  // Set once the active-tab query settles, so the Review button cannot be
  // clicked while tabId is still null from a pending query.
  let tabReady = $state(false);
  // Site-neutral: any web page can be inspected via temporary user-activated
  // injection, so support is a scheme check, not a hostname allowlist.
  let canInspect = $state(false);
  let busy = $state(false);
  let error = $state('');
  let warning = $state('');
  let progress = $state<ImportProgress | null>(null);
  // Operator review state: preview from the active tab, editable fields,
  // selected photo URLs, per-origin host access, manual entry fallback, and
  // operator-selected local files (uploaded as user-supplied evidence).
  let previewState = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
  let preview = $state<ExtractedListing | null>(null);
  let previewError = $state('');
  let editTitle = $state('');
  let editYear = $state('');
  let editMake = $state('');
  let editModel = $state('');
  let editTrim = $state('');
  let editDescription = $state('');
  let selectedPhotos = $state<string[]>([]);
  let hostAccess = $state<Record<string, boolean>>({});
  let showManual = $state(false);
  let manualTitle = $state('');
  let manualYear = $state('');
  let manualMake = $state('');
  let manualModel = $state('');
  let manualTrim = $state('');
  let manualDescription = $state('');
  let userFiles = $state<UserSuppliedFile[]>([]);
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
      canInspect = tabUrl.startsWith('http://') || tabUrl.startsWith('https://');
    } catch {
      canInspect = false;
    } finally {
      tabReady = true;
    }
  }

  function originOf(url: string): string {
    try {
      return new URL(url).origin;
    } catch {
      return '';
    }
  }

  function resetReview() {
    previewState = 'idle';
    preview = null;
    previewError = '';
    editTitle = editYear = editMake = editModel = editTrim = editDescription = '';
    selectedPhotos = [];
    hostAccess = {};
    showManual = false;
    userFiles = [];
  }

  async function reviewListing() {
    if (tabId === null) {
      previewError = 'No active tab.';
      previewState = 'error';
      return;
    }
    previewState = 'loading';
    previewError = '';
    preview = null;
    showManual = false;
    try {
      const res = (await browser.runtime.sendMessage({
        type: 'vcc:preview-extract',
        tabId,
      })) as PreviewResponse;
      if (!res || res.type === 'vcc:error' || !isExtractedListing(res.listing)) {
        previewState = 'error';
        previewError =
          res && res.type === 'vcc:error' && res.message
            ? res.message
            : 'No vehicle detected on this page.';
        return;
      }
      preview = res.listing;
      previewState = 'ready';
      editTitle = preview.title;
      editYear = preview.year === null ? '' : String(preview.year);
      editMake = preview.make;
      editModel = preview.model;
      editTrim = preview.trim;
      editDescription = preview.description;
      // Default: every detected photo selected; the operator deselects.
      selectedPhotos = preview.photos.map((p) => p.url);
      await refreshHostAccess();
    } catch (err) {
      previewState = 'error';
      previewError = err instanceof Error ? err.message : 'Preview failed.';
    }
  }

  async function refreshHostAccess() {
    const origins = [...new Set(selectedPhotos.map(originOf).filter((o) => o !== ''))];
    if (origins.length === 0) {
      hostAccess = {};
      return;
    }
    try {
      const res = (await browser.runtime.sendMessage({
        type: 'vcc:check-hosts',
        origins,
      })) as HostAccessResponse;
      if (res && res.type === 'vcc:host-access') hostAccess = res.access;
    } catch {
      hostAccess = {};
    }
  }

  async function requestHostAccess(origin: string) {
    error = '';
    try {
      const granted = await browser.permissions.request({ origins: [`${origin}/*`] });
      if (!granted) {
        error = `Access to ${origin} was not granted; its photos will fail to download.`;
      }
      await refreshHostAccess();
    } catch (err) {
      error = err instanceof Error ? err.message : `Could not request access to ${origin}.`;
    }
  }

  function togglePhoto(url: string) {
    selectedPhotos = selectedPhotos.includes(url)
      ? selectedPhotos.filter((u) => u !== url)
      : [...selectedPhotos, url];
    void refreshHostAccess();
  }

  async function readUserFiles(event: Event) {
    error = '';
    const input = event.target as HTMLInputElement | null;
    const files = input?.files ? Array.from(input.files) : [];
    if (input) input.value = '';
    for (const file of files) {
      if (userFiles.length >= MAX_USER_FILES) {
        error = `At most ${MAX_USER_FILES} user-supplied files per import.`;
        break;
      }
      if (!file.type.startsWith('image/')) {
        error = `${file.name}: only image files are accepted.`;
        continue;
      }
      if (file.size > 15 * 1024 * 1024) {
        error = `${file.name}: file exceeds the 15 MiB photo limit.`;
        continue;
      }
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
        reader.onerror = () => reject(new Error('could not read file'));
        reader.readAsDataURL(file);
      }).catch(() => '');
      if (!dataUrl.startsWith('data:image/')) {
        error = `${file.name}: could not be read as an image.`;
        continue;
      }
      userFiles = [...userFiles, { name: file.name.slice(0, 120) || 'photo', dataUrl }];
    }
  }

  function removeUserFile(index: number) {
    userFiles = userFiles.filter((_, i) => i !== index);
  }

  function parseYearField(raw: string, fieldName: string): number | null {
    const trimmed = raw.trim();
    if (trimmed === '') return null;
    if (!/^\d{4}$/.test(trimmed)) throw new Error(`${fieldName} must be a 4-digit year.`);
    return Number(trimmed);
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
    message:
      | {
          type: 'vcc:start-import';
          tabId: number;
          requestedCheck?: RequestedCheck | null;
          overrides?: {
            title?: string;
            year?: number | null;
            make?: string;
            model?: string;
            trim?: string;
            description?: string;
          };
          photoUrls?: string[];
          manualEntry?: ManualEntry | null;
          userFiles?: UserSuppliedFile[];
        }
      | { type: 'vcc:retry-failed' },
    failureLabel: string,
  ) {
    error = '';
    warning = '';
    busy = true;
    startPolling();
    try {
      const res = (await browser.runtime.sendMessage(message)) as {
        ok: boolean;
        error?: string;
        warning?: string;
        progress?: ImportProgress;
      };
      if (!res?.ok) error = res?.error ?? failureLabel;
      else {
        if (res.progress) progress = res.progress;
        // A warning (e.g. a user-supplied upload that failed) never masks
        // the import outcome, but the operator must see it.
        if (res.warning) warning = res.warning;
      }
    } catch (err) {
      error = err instanceof Error ? err.message : failureLabel;
    } finally {
      busy = false;
      await refreshAll();
      if (!progress || progress.status !== 'active') stopPolling();
    }
  }

  /** Build the start-import message from the review state. Changed fields
   * become overrides; untouched fields keep detected provenance. Claims are
   * never typed: ACC stays the operator-requested check. */
  function buildStartMessage(requestedCheck: RequestedCheck | null): {
    type: 'vcc:start-import';
    tabId: number;
    requestedCheck: RequestedCheck | null;
    overrides?: {
      title?: string;
      year?: number | null;
      make?: string;
      model?: string;
      trim?: string;
      description?: string;
    };
    photoUrls?: string[];
    manualEntry?: ManualEntry | null;
    userFiles?: UserSuppliedFile[];
  } {
    if (tabId === null) throw new Error('No active tab.');
    if (preview && previewState === 'ready' && !showManual) {
      const overrides: Record<string, string | number | null> = {};
      if (editTitle.trim() !== preview.title) overrides['title'] = editTitle.trim();
      const year = parseYearField(editYear, 'Year');
      if (year !== preview.year) overrides['year'] = year;
      if (editMake.trim() !== preview.make) overrides['make'] = editMake.trim();
      if (editModel.trim() !== preview.model) overrides['model'] = editModel.trim();
      if (editTrim.trim() !== preview.trim) overrides['trim'] = editTrim.trim();
      if (editDescription.trim() !== preview.description)
        overrides['description'] = editDescription.trim();
      return {
        type: 'vcc:start-import',
        tabId,
        requestedCheck,
        ...(Object.keys(overrides).length > 0 ? { overrides } : {}),
        photoUrls: selectedPhotos,
        userFiles,
      };
    }
    const manualEntry: ManualEntry = {
      title: manualTitle.trim() || 'Untitled vehicle',
      year: parseYearField(manualYear, 'Year'),
      make: manualMake.trim(),
      model: manualModel.trim(),
      trim: manualTrim.trim(),
      description: manualDescription.trim(),
    };
    return { type: 'vcc:start-import', tabId, requestedCheck, manualEntry, userFiles };
  }

  async function startImport(requestedCheck?: RequestedCheck | null) {
    try {
      await runRemote(buildStartMessage(requestedCheck ?? null), 'Import failed.');
    } catch (err) {
      error = err instanceof Error ? err.message : 'Import failed.';
    }
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
      {:else if canInspect}
        <p class="break-all"><span class="font-medium">Page to inspect:</span> {tabUrl}</p>
      {:else}
        <p class="text-slate-500">Open a vehicle listing page (http/https), then review it here.</p>
      {/if}
    </div>

    {#if previewState === 'idle' || previewState === 'loading'}
      <button
        class="mt-3 w-full rounded-md bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50"
        type="button"
        disabled={busy || !tabReady || !canInspect || previewState === 'loading'}
        onclick={() => void reviewListing()}
        title="Inspect the active tab and review the detected vehicle before importing"
      >
        {previewState === 'loading' ? 'Inspecting page…' : 'Review listing'}
      </button>
    {:else if previewState === 'error'}
      <div class="mt-3 rounded-md border border-amber-200 bg-amber-50 p-2 text-xs" role="alert">
        {previewError}
      </div>
      <div class="mt-2 flex gap-2">
        <button
          class="flex-1 rounded-md border border-slate-300 px-3 py-1.5 text-xs disabled:opacity-50"
          type="button"
          disabled={busy || !canInspect}
          onclick={() => void reviewListing()}
        >
          Retry inspection
        </button>
        <button
          class="flex-1 rounded-md border border-slate-300 px-3 py-1.5 text-xs disabled:opacity-50"
          type="button"
          disabled={busy}
          onclick={() => {
            showManual = true;
            previewState = 'ready';
            previewError = '';
          }}
        >
          Enter manually
        </button>
      </div>
    {/if}

    {#if previewState === 'ready'}
      {#if preview && !showManual}
        {@const listing = preview}
        <div class="mt-3 rounded-md border border-slate-200 p-2 text-xs">
          <div class="flex items-center gap-2">
            <span
              class="rounded-full px-2 py-0.5"
              class:bg-emerald-100={listing.extraction?.confidence === 'high'}
              class:text-emerald-800={listing.extraction?.confidence === 'high'}
              class:bg-amber-100={listing.extraction?.confidence !== 'high'}
              class:text-amber-800={listing.extraction?.confidence !== 'high'}
            >
              {listing.extraction?.confidence ?? 'low'} confidence · {listing.extractorId}
            </span>
          </div>
          {#if (listing.extraction?.reasons ?? []).length > 0}
            <ul class="mt-1 list-disc pl-4 text-slate-600">
              {#each listing.extraction?.reasons ?? [] as reason, i (i)}
                <li>{reason}</li>
              {/each}
            </ul>
          {/if}
          {#if listing.advertisedClaims.length > 0}
            <p class="mt-1 text-slate-600">
              Seller claims ({listing.advertisedClaims.length}): {listing.advertisedClaims
                .slice(0, 3)
                .join(' · ')}{listing.advertisedClaims.length > 3 ? ' ·…' : ''}
            </p>
          {:else}
            <p class="mt-1 text-slate-600">No seller equipment claims detected.</p>
          {/if}
        </div>

        <div class="mt-2 grid grid-cols-2 gap-2 text-xs">
          <label class="col-span-2 block">
            <span class="font-medium">Title</span>
            <input
              class="mt-0.5 w-full rounded-md border border-slate-300 px-2 py-1"
              type="text"
              bind:value={editTitle}
            />
          </label>
          <label class="block">
            <span class="font-medium">Year</span>
            <input
              class="mt-0.5 w-full rounded-md border border-slate-300 px-2 py-1"
              type="text"
              inputmode="numeric"
              placeholder="e.g. 2021"
              bind:value={editYear}
            />
          </label>
          <label class="block">
            <span class="font-medium">Trim</span>
            <input
              class="mt-0.5 w-full rounded-md border border-slate-300 px-2 py-1"
              type="text"
              bind:value={editTrim}
            />
          </label>
          <label class="block">
            <span class="font-medium">Make</span>
            <input
              class="mt-0.5 w-full rounded-md border border-slate-300 px-2 py-1"
              type="text"
              bind:value={editMake}
            />
          </label>
          <label class="block">
            <span class="font-medium">Model</span>
            <input
              class="mt-0.5 w-full rounded-md border border-slate-300 px-2 py-1"
              type="text"
              bind:value={editModel}
            />
          </label>
          <label class="col-span-2 block">
            <span class="font-medium">Description</span>
            <textarea
              class="mt-0.5 w-full rounded-md border border-slate-300 px-2 py-1"
              rows="2"
              bind:value={editDescription}></textarea>
          </label>
        </div>
        <p class="mt-1 text-xs text-slate-500">
          Corrected fields are marked operator-entered. Seller claims are shown, never edited.
        </p>

        <div class="mt-2 text-xs font-medium">
          Photos ({selectedPhotos.length}/{listing.photos.length} selected)
        </div>
        {#if listing.photos.length === 0}
          <p class="mt-1 text-xs text-slate-500">
            No page photos detected. You can still attach your own images below.
          </p>
        {:else}
          <ul class="mt-1 max-h-40 space-y-1 overflow-y-auto text-xs">
            {#each listing.photos as photo (photo.url)}
              {@const origin = originOf(photo.url)}
              <li class="flex items-center gap-2 rounded border border-slate-200 px-2 py-1">
                <input
                  type="checkbox"
                  checked={selectedPhotos.includes(photo.url)}
                  onchange={() => togglePhoto(photo.url)}
                  aria-label={`Include photo ${photo.url}`}
                />
                <span class="min-w-0 flex-1 truncate" title={photo.url}>{photo.url}</span>
                {#if origin !== '' && hostAccess[origin] === false}
                  <button
                    class="shrink-0 underline"
                    type="button"
                    onclick={() => void requestHostAccess(origin)}
                    title={`Allow photo downloads from ${origin}`}
                  >
                    Allow host
                  </button>
                {/if}
              </li>
            {/each}
          </ul>
          {#if Object.entries(hostAccess).some(([, allowed]) => !allowed)}
            <p class="mt-1 text-xs text-amber-700">
              Some photo hosts need one-time access. Use “Allow host”, or those photos will be
              recorded as permission failures.
            </p>
          {/if}
        {/if}
      {/if}

      {#if showManual || !preview}
        <div class="mt-2 grid grid-cols-2 gap-2 text-xs">
          <p class="col-span-2 text-slate-600">
            {#if showManual && preview}
              Detection was uncertain — confirm or enter the vehicle below. This page ({tabUrl})
              stays linked as the source.
            {:else}
              No vehicle detected on this page ({tabUrl || 'unknown page'}). Enter it manually;
              attach your own photos below — they are labelled user-supplied, never page evidence.
            {/if}
          </p>
          <label class="col-span-2 block">
            <span class="font-medium">Title</span>
            <input
              class="mt-0.5 w-full rounded-md border border-slate-300 px-2 py-1"
              type="text"
              bind:value={manualTitle}
            />
          </label>
          <label class="block">
            <span class="font-medium">Year</span>
            <input
              class="mt-0.5 w-full rounded-md border border-slate-300 px-2 py-1"
              type="text"
              inputmode="numeric"
              placeholder="e.g. 2021"
              bind:value={manualYear}
            />
          </label>
          <label class="block">
            <span class="font-medium">Trim</span>
            <input
              class="mt-0.5 w-full rounded-md border border-slate-300 px-2 py-1"
              type="text"
              bind:value={manualTrim}
            />
          </label>
          <label class="block">
            <span class="font-medium">Make</span>
            <input
              class="mt-0.5 w-full rounded-md border border-slate-300 px-2 py-1"
              type="text"
              bind:value={manualMake}
            />
          </label>
          <label class="block">
            <span class="font-medium">Model</span>
            <input
              class="mt-0.5 w-full rounded-md border border-slate-300 px-2 py-1"
              type="text"
              bind:value={manualModel}
            />
          </label>
          <label class="col-span-2 block">
            <span class="font-medium">Description</span>
            <textarea
              class="mt-0.5 w-full rounded-md border border-slate-300 px-2 py-1"
              rows="2"
              bind:value={manualDescription}></textarea>
          </label>
        </div>
      {/if}

      {#if preview && !showManual}
        <button
          class="mt-2 w-full text-xs text-slate-600 underline disabled:opacity-50"
          type="button"
          disabled={busy}
          onclick={() => {
            showManual = true;
          }}
        >
          Detection looks wrong? Enter manually instead
        </button>
      {/if}

      <div class="mt-2 text-xs font-medium">Your photos (user-supplied, {userFiles.length})</div>
      <input
        class="mt-1 block w-full text-xs"
        type="file"
        accept="image/*"
        multiple
        aria-label="Attach your own photos"
        onchange={(e) => void readUserFiles(e)}
      />
      {#if userFiles.length > 0}
        <ul class="mt-1 space-y-1 text-xs">
          {#each userFiles as file, i (i)}
            <li class="flex items-center gap-2 rounded border border-slate-200 px-2 py-1">
              <span class="min-w-0 flex-1 truncate">{file.name}</span>
              <button class="underline" type="button" onclick={() => removeUserFile(i)}>
                Remove
              </button>
            </li>
          {/each}
        </ul>
      {/if}

      <button
        class="mt-3 w-full rounded-md bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50"
        type="button"
        disabled={busy}
        onclick={() => void startImport('adaptive_cruise_control')}
        title="Import and mark adaptive cruise control as the operator-requested evaluation target"
      >
        {busy ? 'Importing…' : 'Verify Adaptive Cruise Control'}
      </button>
      <button
        class="mt-2 w-full rounded-md border border-slate-300 px-3 py-1.5 text-slate-700 disabled:opacity-50"
        type="button"
        disabled={busy}
        onclick={() => void startImport()}
        title="Import without a requested check"
      >
        Import photos
      </button>
      <button
        class="mt-2 w-full text-xs text-slate-600 underline disabled:opacity-50"
        type="button"
        disabled={busy}
        onclick={() => resetReview()}
      >
        Inspect a different page
      </button>
    {/if}

    {#if progress && progress.status === 'active'}
      {@const c = counts()}
      {#if operationRunning === false}
        <div class="mt-2 rounded-md bg-amber-50 p-2 text-xs" role="status">
          Import interrupted — no upload is running. Stored photos and the listing are kept.
          <div class="mt-2 flex gap-2">
            <button
              class="rounded-md bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50"
              type="button"
              disabled={busy || !canInspect}
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
          {#if !canInspect}
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
  {#if warning}
    <p class="mt-2 rounded-md bg-amber-50 p-2 text-xs text-amber-800" role="status">{warning}</p>
  {/if}
</div>
