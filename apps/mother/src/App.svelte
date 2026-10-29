<script lang="ts">
  import { onMount } from 'svelte';
  import { backend } from './lib/api';
  import Dashboard from './lib/Dashboard.svelte';
  import ListingDetail from './lib/ListingDetail.svelte';
  import ManualImport from './lib/ManualImport.svelte';
  import Pairing from './lib/Pairing.svelte';
  import Setup from './lib/Setup.svelte';

  let route = $state(window.location.hash || '#/');
  let backendOk = $state<boolean | null>(null);
  let initialized = $state<boolean | null>(null);
  let bootError = $state('');

  function onHashChange() {
    route = window.location.hash || '#/';
  }

  async function boot() {
    try {
      await backend.health();
      backendOk = true;
    } catch {
      backendOk = false;
      bootError = 'Backend unreachable at /api (is Django running on 127.0.0.1:8000?).';
      return;
    }
    try {
      const status = await backend.setupStatus();
      initialized = status.initialized;
      if (!status.initialized && !route.startsWith('#/setup')) {
        window.location.hash = '#/setup';
      }
    } catch {
      bootError = 'Could not read setup status.';
    }
  }

  onMount(() => {
    window.addEventListener('hashchange', onHashChange);
    void boot();
    return () => window.removeEventListener('hashchange', onHashChange);
  });

  function refreshSetup() {
    void boot();
  }

  const listingMatch = $derived(route.match(/^#\/listings\/([0-9a-f-]+)/i));
</script>

<div class="min-h-screen bg-slate-100 text-slate-900">
  <header class="border-b border-slate-200 bg-white">
    <div class="mx-auto flex max-w-5xl items-center gap-6 px-6 py-4">
      <a href="#/" class="text-lg font-semibold tracking-tight">Verified Cruise Control</a>
      <nav class="flex gap-4 text-sm" aria-label="Primary">
        <a class="text-slate-600 hover:text-slate-900 hover:underline" href="#/">Dashboard</a>
        <a class="text-slate-600 hover:text-slate-900 hover:underline" href="#/import"
          >Manual import</a
        >
        <a class="text-slate-600 hover:text-slate-900 hover:underline" href="#/pairing"
          >Extension pairing</a
        >
      </nav>
      <div class="ml-auto text-sm" role="status" aria-label="Backend status">
        {#if backendOk === null}
          <span class="text-slate-400">connecting…</span>
        {:else if backendOk}
          <span class="rounded-full bg-emerald-100 px-3 py-1 text-emerald-800"
            >backend connected</span
          >
        {:else}
          <span class="rounded-full bg-red-100 px-3 py-1 text-red-800">backend unreachable</span>
        {/if}
      </div>
    </div>
  </header>

  <main class="mx-auto max-w-5xl px-6 py-8">
    {#if bootError}
      <div class="rounded-md border border-red-200 bg-red-50 p-4 text-red-800" role="alert">
        {bootError}
      </div>
    {:else if route.startsWith('#/setup')}
      <Setup onDone={refreshSetup} />
    {:else if listingMatch}
      <ListingDetail id={listingMatch[1]} />
    {:else if route.startsWith('#/import')}
      <ManualImport />
    {:else if route.startsWith('#/pairing')}
      <Pairing />
    {:else}
      <Dashboard gateSetup={initialized === false} />
    {/if}
    <footer class="mt-12 border-t border-slate-200 pt-4 text-xs text-slate-500">
      PR #1 foundation: listings and original photos are imported evidence. No AI audit has run —
      nothing here is AI-verified.
    </footer>
  </main>
</div>
