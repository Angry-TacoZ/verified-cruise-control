<script lang="ts">
  import { onMount } from 'svelte';
  import { ApiError, backend, type PairingInfo } from './api';

  let pairings = $state<PairingInfo[]>([]);
  let loading = $state(true);
  let error = $state('');
  let needsSetup = $state(false);
  let newToken = $state('');
  let working = $state(false);
  let copied = $state(false);

  async function load() {
    loading = true;
    error = '';
    try {
      pairings = await backend.pairingStatus();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) needsSetup = true;
      else error = err instanceof Error ? err.message : 'Failed to load pairings.';
    } finally {
      loading = false;
    }
  }

  async function rotate() {
    error = '';
    newToken = '';
    working = true;
    try {
      const res = await backend.rotatePairing('manual rotation');
      newToken = res.extension_token;
      await load();
    } catch (err) {
      error = err instanceof Error ? err.message : 'Rotation failed.';
    } finally {
      working = false;
    }
  }

  async function revoke(id: string) {
    error = '';
    working = true;
    try {
      await backend.revokePairing(id);
      await load();
    } catch (err) {
      error = err instanceof Error ? err.message : 'Revocation failed.';
    } finally {
      working = false;
    }
  }

  async function copyToken() {
    try {
      await navigator.clipboard.writeText(newToken);
      copied = true;
      setTimeout(() => (copied = false), 2000);
    } catch {
      copied = false;
    }
  }

  function fmt(date: string | null): string {
    return date ? new Date(date).toLocaleString() : '—';
  }

  onMount(() => void load());
</script>

<section aria-labelledby="pairing-title">
  <h1 id="pairing-title" class="text-2xl font-semibold">Extension pairing</h1>
  <p class="mt-1 max-w-2xl text-sm text-slate-600">
    Pair the browser extension once: paste a token below into the extension popup on this machine.
    Tokens authenticate extension uploads only — the mother app itself uses this browser session.
  </p>

  {#if needsSetup}
    <div class="mt-4 rounded-md border border-amber-200 bg-amber-50 p-4 text-amber-900">
      Sign in first on the <a class="underline" href="#/setup">setup page</a>.
    </div>
  {:else if loading}
    <p class="mt-4 text-slate-500">Loading…</p>
  {:else}
    {#if error}
      <div class="mt-4 rounded-md border border-red-200 bg-red-50 p-4 text-red-800" role="alert">
        {error}
      </div>
    {/if}
    <div class="mt-4">
      <button
        class="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        type="button"
        onclick={() => void rotate()}
        disabled={working}
      >
        {working ? 'Working…' : 'Revoke all and issue new token'}
      </button>
    </div>

    {#if newToken}
      <div class="mt-4 max-w-2xl rounded-md border border-amber-300 bg-amber-50 p-4">
        <h2 class="font-semibold text-amber-900">New pairing token — shown once</h2>
        <div class="mt-2 flex items-center gap-2">
          <code class="flex-1 break-all rounded bg-white px-2 py-1 font-mono text-xs"
            >{newToken}</code
          >
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

    <h2 class="mt-6 text-lg font-semibold">Pairings</h2>
    {#if pairings.length === 0}
      <p class="mt-2 text-sm text-slate-500">No pairings yet.</p>
    {:else}
      <ul class="mt-2 divide-y divide-slate-200 rounded-md border border-slate-200 bg-white">
        {#each pairings as pairing (pairing.id)}
          <li class="flex items-center gap-4 px-4 py-3 text-sm">
            <span
              class="rounded-full px-2 py-0.5 text-xs"
              class:bg-emerald-100={pairing.active}
              class:text-emerald-800={pairing.active}
              class:bg-slate-200={!pairing.active}
              class:text-slate-600={!pairing.active}
            >
              {pairing.active ? 'active' : 'revoked'}
            </span>
            <span class="font-mono text-xs text-slate-500">{pairing.id.slice(0, 8)}…</span>
            <span class="text-slate-600">{pairing.label || '—'}</span>
            <span class="ml-auto text-slate-500">last used {fmt(pairing.last_used_at)}</span>
            {#if pairing.active}
              <button
                class="rounded-md border border-slate-300 px-2 py-1 text-xs disabled:opacity-50"
                type="button"
                onclick={() => void revoke(pairing.id)}
                disabled={working}
              >
                Revoke
              </button>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>
