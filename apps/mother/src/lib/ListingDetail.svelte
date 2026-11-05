<script lang="ts">
  import { ApiError, backend, type Listing, type Photo, type PhotoFailure } from './api';
  import { isAccAdvertised, isAccRequested } from './accVerdict';
  import AuditPanel from './AuditPanel.svelte';

  let { id }: { id: string } = $props();

  let listing = $state<Listing | null>(null);
  let photos = $state<Photo[]>([]);
  let failures = $state<PhotoFailure[]>([]);
  let loading = $state(true);
  let error = $state('');

  function vehicleLine(l: Listing): string {
    return [l.year, l.make, l.model, l.trim].filter(Boolean).join(' ') || 'Unknown vehicle';
  }

  /** Shorten a long URL for display; the full URL stays on the link itself. */
  function shortUrl(url: string): string {
    return url.length <= 48 ? url : `${url.slice(0, 48)}…`;
  }

  /**
   * VIN for the source box. The listing row may not carry one, in which case
   * fall back to the VIN printed in the seller's own claim texts — same
   * source, so the two places always agree.
   */
  function displayVin(l: Listing): string {
    if (l.vin) return l.vin;
    for (const claim of l.advertised_claims ?? []) {
      for (const token of claim.split(/[^A-Za-z0-9]+/)) {
        if (/^[A-HJ-NPR-Z0-9]{17}$/.test(token)) return token;
      }
    }
    return '—';
  }

  function kindLabel(kind: PhotoFailure['kind']): string {
    switch (kind) {
      case 'download':
        return 'download failed';
      case 'permission':
        return 'host permission denied';
      case 'validation':
        return 'validation failed';
      case 'upload':
        return 'upload rejected';
    }
  }

  async function load(listingId: string) {
    loading = true;
    error = '';
    try {
      listing = await backend.getListing(listingId);
      photos = await backend.listPhotos(listingId);
      failures = await backend.listFailures(listingId);
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

  // Reload whenever the routed listing id changes: Svelte reuses this
  // component instance across `#/listings/:id` navigations, so onMount alone
  // would leave the previous listing (and its failures) on screen.
  $effect(() => {
    listing = null;
    photos = [];
    failures = [];
    void load(id);
  });
</script>

<section aria-labelledby="detail">
  <a class="text-sm text-[#6f5f4c] underline" href="#/">← Back to listings</a>
  {#if loading}
    <p class="mt-4 text-[#8a7660]">Loading…</p>
  {:else if error || !listing}
    <div class="mt-4 rounded-md border border-red-200 bg-red-50 p-4 text-red-800" role="alert">
      {error || 'Listing not found.'}
    </div>
  {:else}
    <h1 id="detail" class="mt-2 text-2xl font-semibold">{listing.title}</h1>
    <p class="mt-1 text-sm text-[#6f5f4c]">{vehicleLine(listing)}</p>
    <dl
      class="mt-4 grid grid-cols-2 gap-x-8 gap-y-2 rounded-md border border-[#e6c795] bg-[#fff8ef] p-4 text-sm md:grid-cols-3"
    >
      <div>
        <dt class="text-[#8a7660]">Source</dt>
        <dd class="break-all">{listing.source_site}</dd>
      </div>
      <div>
        <dt class="text-[#8a7660]">Source URL</dt>
        <dd class="truncate">
          <a class="underline" href={listing.source_url} title={listing.source_url}
            >{shortUrl(listing.source_url)}</a
          >
        </dd>
      </div>
      <div>
        <dt class="text-[#8a7660]">VIN</dt>
        <dd>{displayVin(listing)}</dd>
      </div>
      <div>
        <dt class="text-[#8a7660]">Import status</dt>
        <dd>{listing.import_status.replace('_', ' ')}</dd>
      </div>
      <div>
        <dt class="text-[#8a7660]">Photos</dt>
        <dd>{listing.photo_count} stored</dd>
      </div>
      <div>
        <dt class="text-[#8a7660]">Failed uploads</dt>
        <dd>
          {listing.failed_upload_count}{#if listing.failed_upload_count > 0}
            <span class="text-amber-700"> — import marked “partial”</span>
          {/if}
        </dd>
      </div>
    </dl>

    {#if failures.length > 0}
      {@const outstanding = failures.filter((f) => !f.resolved)}
      <h2 class="mt-6 text-lg font-semibold">Import failures ({failures.length})</h2>
      {#if outstanding.length === 0}
        <p class="mt-2 text-sm text-emerald-800">
          All reported failures recovered on retry — evidence is complete.
        </p>
      {:else}
        <p class="mt-2 text-sm text-[#6f5f4c]">
          {outstanding.length} photo{outstanding.length === 1 ? '' : 's'} couldn't be imported — the audit
          runs on the {photos.length} available. No action needed unless evidence is missing.
        </p>
      {/if}
      <details class="mt-2 text-sm">
        <summary class="cursor-pointer text-[#6f5f4c] underline">Show per-photo details</summary>
        <ul class="mt-2 space-y-2">
          {#each failures as failure (failure.id)}
            <li
              class="rounded-md border p-3 {failure.resolved
                ? 'border-emerald-200 bg-emerald-50 text-emerald-900'
                : 'border-amber-200 bg-amber-50 text-amber-900'}"
            >
              <span class="font-medium"
                >{kindLabel(failure.kind)}{failure.resolved ? ' (recovered)' : ''}</span
              >
              {#if failure.detail}
                <span> — {failure.detail}</span>
              {/if}
              {#if failure.source_url}
                <div class="mt-1 break-all font-mono text-xs">{failure.source_url}</div>
              {/if}
            </li>
          {/each}
        </ul>
      </details>
    {/if}

    <h2 class="mt-6 text-lg font-semibold">Advertised claims</h2>
    {#if listing.advertised_claims.length === 0}
      <p class="text-sm text-[#8a7660]">None recorded.</p>
    {:else}
      <ul class="mt-2 list-disc pl-6 text-sm">
        {#each listing.advertised_claims as claim (claim)}
          <li>{claim}</li>
        {/each}
      </ul>
    {/if}

    <h2 class="mt-6 text-lg font-semibold">ACC evaluation target</h2>
    {#if isAccAdvertised(listing.advertised_claims) && isAccRequested(listing.requested_checks)}
      <p class="mt-2 text-sm text-[#3a2f23]">
        Seller-advertised <span class="font-medium">and</span> operator-requested: the audit evaluates
        the seller's ACC claim.
      </p>
    {:else if isAccAdvertised(listing.advertised_claims)}
      <p class="mt-2 text-sm text-[#3a2f23]">
        Seller-advertised: the listing text claims adaptive cruise control.
      </p>
    {:else if isAccRequested(listing.requested_checks)}
      <p class="mt-2 text-sm text-[#3a2f23]">
        Operator-requested: the seller did not advertise ACC. This check was selected in the
        extension and is <span class="font-medium">not</span> a seller claim.
      </p>
    {:else}
      <p class="mt-2 text-sm text-[#8a7660]">No ACC check recorded for this listing.</p>
    {/if}

    <h2 class="mt-6 text-lg font-semibold">Original description</h2>
    <p class="mt-2 whitespace-pre-wrap rounded-md border border-[#e6c795] bg-[#fff8ef] p-4 text-sm">
      {listing.description || '—'}
    </p>

    <h2 class="mt-6 text-lg font-semibold">Original photographs ({photos.length})</h2>
    {#if photos.length === 0}
      <p class="mt-2 text-sm text-[#8a7660]">No photographs stored for this listing.</p>
    {:else}
      <ul class="mt-2 grid grid-cols-2 gap-4 md:grid-cols-3">
        {#each photos as photo (photo.id)}
          <li class="overflow-hidden rounded-md border border-[#e6c795] bg-[#fff8ef]">
            <img
              src={backend.photoUrl(listing.id, photo.id)}
              alt={`Original listing photo ${photo.original_filename}`}
              class="aspect-[4/3] w-full object-contain bg-slate-950"
              loading="lazy"
            />
            <div class="p-2 text-xs text-[#6f5f4c]">
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

    <AuditPanel
      listingId={listing.id}
      advertisedClaims={listing.advertised_claims}
      requestedChecks={listing.requested_checks ?? []}
    />
  {/if}
</section>
