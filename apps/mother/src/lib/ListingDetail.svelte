<script lang="ts">
  import { onMount } from 'svelte';
  import { ApiError, backend, type Listing, type Photo } from './api';

  let { id }: { id: string } = $props();

  let listing = $state<Listing | null>(null);
  let photos = $state<Photo[]>([]);
  let loading = $state(true);
  let error = $state('');

  function vehicleLine(l: Listing): string {
    return [l.year, l.make, l.model, l.trim].filter(Boolean).join(' ') || 'Unknown vehicle';
  }

  async function load() {
    loading = true;
    error = '';
    try {
      listing = await backend.getListing(id);
      photos = await backend.listPhotos(id);
    } catch (err) {
      error =
        err instanceof ApiError && err.status === 404
          ? 'Listing not found.'
          : err instanceof Error
            ? err.message
            : 'Failed to load listing.';
    } finally {
      loading = false;
    }
  }

  onMount(() => void load());
</script>

<section aria-labelledby="detail">
  <a class="text-sm text-slate-600 underline" href="#/">← Back to listings</a>
  {#if loading}
    <p class="mt-4 text-slate-500">Loading…</p>
  {:else if error || !listing}
    <div class="mt-4 rounded-md border border-red-200 bg-red-50 p-4 text-red-800" role="alert">
      {error || 'Listing not found.'}
    </div>
  {:else}
    <h1 id="detail" class="mt-2 text-2xl font-semibold">{listing.title}</h1>
    <p class="mt-1 text-sm text-slate-600">{vehicleLine(listing)}</p>
    <dl
      class="mt-4 grid grid-cols-2 gap-x-8 gap-y-2 rounded-md border border-slate-200 bg-white p-4 text-sm md:grid-cols-3"
    >
      <div>
        <dt class="text-slate-500">Source</dt>
        <dd class="break-all">{listing.source_site}</dd>
      </div>
      <div>
        <dt class="text-slate-500">Source URL</dt>
        <dd class="break-all">
          <a class="underline" href={listing.source_url}>{listing.source_url}</a>
        </dd>
      </div>
      <div>
        <dt class="text-slate-500">VIN</dt>
        <dd>{listing.vin ?? '—'}</dd>
      </div>
      <div>
        <dt class="text-slate-500">Import status</dt>
        <dd>{listing.import_status.replace('_', ' ')}</dd>
      </div>
      <div>
        <dt class="text-slate-500">Photos</dt>
        <dd>{listing.photo_count} stored</dd>
      </div>
      <div>
        <dt class="text-slate-500">Failed uploads</dt>
        <dd>
          {listing.failed_upload_count}{#if listing.failed_upload_count > 0}
            <span class="text-amber-700"> — import marked “partial”</span>
          {/if}
        </dd>
      </div>
    </dl>

    <h2 class="mt-6 text-lg font-semibold">Advertised claims</h2>
    {#if listing.advertised_claims.length === 0}
      <p class="text-sm text-slate-500">None recorded.</p>
    {:else}
      <ul class="mt-2 list-disc pl-6 text-sm">
        {#each listing.advertised_claims as claim (claim)}
          <li>{claim}</li>
        {/each}
      </ul>
    {/if}

    <h2 class="mt-6 text-lg font-semibold">Original description</h2>
    <p class="mt-2 whitespace-pre-wrap rounded-md border border-slate-200 bg-white p-4 text-sm">
      {listing.description || '—'}
    </p>

    <h2 class="mt-6 text-lg font-semibold">Original photographs ({photos.length})</h2>
    {#if photos.length === 0}
      <p class="mt-2 text-sm text-slate-500">No photographs stored for this listing.</p>
    {:else}
      <ul class="mt-2 grid grid-cols-2 gap-4 md:grid-cols-3">
        {#each photos as photo (photo.id)}
          <li class="overflow-hidden rounded-md border border-slate-200 bg-white">
            <img
              src={backend.photoUrl(listing.id, photo.id)}
              alt={`Original listing photo ${photo.original_filename}`}
              class="aspect-[4/3] w-full object-contain bg-slate-950"
              loading="lazy"
            />
            <div class="p-2 text-xs text-slate-600">
              <div class="truncate" title={photo.original_filename}>{photo.original_filename}</div>
              <div>{photo.width}×{photo.height} · {photo.image_format} · {photo.provenance}</div>
              <div class="truncate font-mono" title={photo.sha256}>
                sha256 {photo.sha256.slice(0, 16)}…
              </div>
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>
