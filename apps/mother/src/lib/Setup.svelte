<script lang="ts">
  import { onMount } from 'svelte';
  import { ApiError, backend } from './api';

  let { onDone }: { onDone: () => void } = $props();

  let initialized = $state<boolean | null>(null);
  let hasSession = $state(false);
  let setupToken = $state('');
  let pairingToken = $state('');
  let error = $state('');
  let working = $state(false);
  let copied = $state(false);

  async function load() {
    try {
      initialized = (await backend.setupStatus()).initialized;
      if (initialized) {
        try {
          await backend.pairingStatus();
          hasSession = true;
        } catch (err) {
          if (!(err instanceof ApiError && err.status === 401)) throw err;
          hasSession = false;
        }
      }
    } catch (err) {
      error = err instanceof Error ? err.message : 'Could not read setup status.';
    }
  }

  async function initialize(event: SubmitEvent) {
    event.preventDefault();
    error = '';
    pairingToken = '';
    working = true;
    try {
      const res = await backend.initialize(setupToken.trim());
      pairingToken = res.extension_token ?? '';
      initialized = true;
      hasSession = true;
      onDone();
    } catch (err) {
      error =
        err instanceof ApiError && err.status === 403
          ? 'Invalid or expired setup token. Mint a fresh one with: python backend/manage.py setup_token --regenerate'
          : err instanceof Error
            ? err.message
            : 'Setup failed.';
    } finally {
      working = false;
    }
  }

  async function copyToken() {
    try {
      await navigator.clipboard.writeText(pairingToken);
      copied = true;
      setTimeout(() => (copied = false), 2000);
    } catch {
      copied = false;
    }
  }

  onMount(() => void load());
</script>

<section aria-labelledby="setup-title" class="max-w-xl">
  <h1 id="setup-title" class="text-2xl font-semibold">Operator setup</h1>
  {#if initialized === null && !error}
    <p class="mt-4 text-slate-500">Loading…</p>
  {:else if error && initialized === null}
    <div class="mt-4 rounded-md border border-red-200 bg-red-50 p-4 text-red-800" role="alert">
      {error}
    </div>
  {:else if initialized && hasSession && !pairingToken}
    <div class="mt-4 rounded-md border border-emerald-200 bg-emerald-50 p-4 text-emerald-900">
      This browser is signed in as the local operator.
      <a class="underline" href="#/pairing">Manage the extension pairing</a>
      or <a class="underline" href="#/">open the dashboard</a>.
    </div>
  {:else}
    <p class="mt-2 text-sm text-slate-600">
      {#if initialized}
        This browser is not signed in. Mint a single-use re-entry token in your terminal (<code
          class="rounded bg-slate-200 px-1">python backend/manage.py setup_token --regenerate</code
        >) and paste it below.
      {:else}
        First run only: paste the one-time setup token printed by
        <code class="rounded bg-slate-200 px-1">scripts/start-backend.ps1</code>
        in your terminal. This signs in this browser and issues the extension pairing token (shown once).
      {/if}
    </p>
    <form class="mt-4 space-y-3" onsubmit={initialize}>
      <div>
        <label class="block text-sm font-medium" for="setup-token">Setup token</label>
        <input
          id="setup-token"
          class="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-sm"
          type="password"
          autocomplete="off"
          bind:value={setupToken}
          required
        />
      </div>
      {#if error}
        <div
          class="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800"
          role="alert"
        >
          {error}
        </div>
      {/if}
      <button
        class="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        type="submit"
        disabled={working}
      >
        {working ? 'Signing in…' : initialized ? 'Sign in' : 'Sign in and issue pairing token'}
      </button>
    </form>
  {/if}

  {#if pairingToken}
    <div class="mt-6 rounded-md border border-amber-300 bg-amber-50 p-4">
      <h2 class="font-semibold text-amber-900">Extension pairing token — shown once</h2>
      <p class="mt-1 text-sm text-amber-800">
        Paste this into the Verified Cruise Control extension popup, then close this page. It cannot
        be recovered (you can rotate it later).
      </p>
      <div class="mt-2 flex items-center gap-2">
        <code class="flex-1 break-all rounded bg-white px-2 py-1 font-mono text-xs">
          {pairingToken}
        </code>
        <button
          class="rounded-md border border-amber-400 px-3 py-1 text-sm"
          type="button"
          onclick={() => void copyToken()}
        >
          {copied ? 'Copied!' : 'Copy'}
        </button>
      </div>
    </div>
  {/if}
</section>
