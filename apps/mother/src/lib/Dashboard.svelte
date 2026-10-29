<script lang="ts">
  import { onMount } from 'svelte';
  import { ApiError, backend, type Listing } from './api';

  let { gateSetup }: { gateSetup: boolean } = $props();

  let listings = $state<Listing[]>([]);
  let loading = $state(true);
  let error = $state('');
  let needsLogin = $state(false);

  async function load() {
    loading = true;
    error = '';
    try {
      const res = await backend.listListings();
      listings = res.items;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        needsLogin = true;
      } else {
        error = err instanceof Error ? err.message : 'Failed to load listings.';
      }
    } finally {
      loading = false;
    }
  }

  onMount(() => void load());
</script>

<section aria-labelledby="dash-title">
  <h1 id="dash-title" class="text-2xl font-semibold">Listings</h1>
  <p class="mt-1 text-sm text-slate-600">
    {listings.length} stored listing{listings.length === 1 ? '' : 's'} · imported evidence only, no AI
    audit yet
  </p>

  {#if gateSetup}
    <div class="mt-4 rounded-md border border-amber-200 bg-amber-50 p-4 text-amber-900">
      Finish <a class="underline" href="#/setup">operator setup</a> first, then import a listing.
    </div>
  {:else if needsLogin}
    <div class="mt-4 rounded-md border border-amber-200 bg-amber-50 p-4 text-amber-900">
      Your session expired. <a class="underline" href="#/setup">Open setup</a> to sign back in.
    </div>
  {:else if loading}
    <p class="mt-4 text-slate-500">Loading…</p>
  {:else if error}
    <div class="mt-4 rounded-md border border-red-200 bg-red-50 p-4 text-red-800" role="alert">
      {error}
      <button class="ml-2 underline" onclick={() => void load()}>Retry</button>
    </div>
  {:else if listings.length === 0}
    <div class="mt-4 rounded-md border border-slate-200 bg-white p-6 text-slate-600">
      No listings yet. Import one with the
      <a class="underline" href="#/pairing">paired browser extension</a>
      or the <a class="underline" href="#/import">manual import</a> page.
    </div>
  {:else}
    <ul class="mt-4 divide-y divide-slate-200 rounded-md border border-slate-200 bg-white">
      {#each listings as listing (listing.id)}
        <li>
          <a
            class="flex items-center gap-4 px-4 py-3 hover:bg-slate-50"
            href={`#/listings/${listing.id}`}
          >
            <div class="min-w-0 flex-1">
              <div class="truncate font-medium">{listing.title}</div>
              <div class="text-sm text-slate-500">
                {[listing.year, listing.make, listing.model, listing.trim]
                  .filter(Boolean)
                  .join(' ')}
                · {listing.source_site}
              </div>
            </div>
            <span class="text-sm text-slate-500">
              {listing.photo_count} photo{listing.photo_count === 1 ? '' : 's'}
            </span>
            <span
              class="rounded-full px-2 py-0.5 text-xs"
              class:bg-emerald-100={listing.import_status === 'complete'}
              class:text-emerald-800={listing.import_status === 'complete'}
              class:bg-amber-100={listing.import_status !== 'complete'}
              class:text-amber-800={listing.import_status !== 'complete'}
            >
              {listing.import_status.replace('_', ' ')}
            </span>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</section>
