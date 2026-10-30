<script lang="ts">
  import { ApiError, backend } from './api';

  let sourceUrl = $state('');
  let sourceSite = $state('');
  let title = $state('');
  let year = $state('');
  let make = $state('');
  let model = $state('');
  let trim = $state('');
  let vin = $state('');
  let description = $state('');
  let claims = $state('');
  let files = $state<FileList | null>(null);
  let busy = $state(false);
  let error = $state('');
  let progress = $state('');
  let doneId = $state('');

  function inputClass(): string {
    return 'mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm';
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    error = '';
    progress = '';
    doneId = '';
    busy = true;
    try {
      const parsedYear = year.trim() === '' ? null : Number(year);
      if (
        parsedYear !== null &&
        (!Number.isInteger(parsedYear) || parsedYear < 1886 || parsedYear > 2100)
      ) {
        throw new Error('Year must be between 1886 and 2100.');
      }
      const listing = await backend.createListing({
        source_url: sourceUrl.trim(),
        source_site: sourceSite.trim(),
        title: title.trim(),
        year: parsedYear,
        make: make.trim(),
        model: model.trim(),
        trim: trim.trim(),
        vin: vin.trim() === '' ? null : vin.trim(),
        description,
        advertised_claims: claims
          .split('\n')
          .map((c) => c.trim())
          .filter(Boolean),
      });
      const picked = files ? Array.from(files) : [];
      let ok = 0;
      for (const [index, file] of picked.entries()) {
        progress = `Uploading photo ${index + 1} of ${picked.length}…`;
        await backend.uploadPhoto(listing.id, file);
        ok += 1;
      }
      progress =
        picked.length === 0 ? 'Listing created (no photos attached).' : `${ok} photo(s) uploaded.`;
      doneId = listing.id;
    } catch (err) {
      error =
        err instanceof ApiError && err.status === 401
          ? 'Session expired — sign in again on the setup page.'
          : err instanceof Error
            ? err.message
            : 'Import failed.';
    } finally {
      busy = false;
    }
  }
</script>

<section aria-labelledby="import-title" class="max-w-2xl">
  <h1 id="import-title" class="text-2xl font-semibold">Manual import</h1>
  <p class="mt-1 text-sm text-slate-600">
    Fallback for sites the extension cannot read. Creates the listing, then uploads each photo as
    its own request.
  </p>
  <form class="mt-4 space-y-3" onsubmit={submit}>
    <div>
      <label class="block text-sm font-medium" for="mi-url">Source URL *</label>
      <input
        id="mi-url"
        class={inputClass()}
        type="url"
        required
        bind:value={sourceUrl}
        placeholder="https://…"
      />
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="block text-sm font-medium" for="mi-site">Source site *</label>
        <input
          id="mi-site"
          class={inputClass()}
          required
          bind:value={sourceSite}
          placeholder="example-motors.test"
        />
      </div>
      <div>
        <label class="block text-sm font-medium" for="mi-title">Title *</label>
        <input id="mi-title" class={inputClass()} required bind:value={title} />
      </div>
      <div>
        <label class="block text-sm font-medium" for="mi-year">Year</label>
        <input id="mi-year" class={inputClass()} inputmode="numeric" bind:value={year} />
      </div>
      <div>
        <label class="block text-sm font-medium" for="mi-make">Make</label>
        <input id="mi-make" class={inputClass()} bind:value={make} />
      </div>
      <div>
        <label class="block text-sm font-medium" for="mi-model">Model</label>
        <input id="mi-model" class={inputClass()} bind:value={model} />
      </div>
      <div>
        <label class="block text-sm font-medium" for="mi-trim">Trim</label>
        <input id="mi-trim" class={inputClass()} bind:value={trim} />
      </div>
    </div>
    <div>
      <label class="block text-sm font-medium" for="mi-vin">VIN (optional)</label>
      <input id="mi-vin" class={inputClass()} maxlength={17} bind:value={vin} />
    </div>
    <div>
      <label class="block text-sm font-medium" for="mi-desc">Original description</label>
      <textarea id="mi-desc" class={inputClass()} rows={4} bind:value={description}></textarea>
    </div>
    <div>
      <label class="block text-sm font-medium" for="mi-claims"
        >Advertised claims (one per line)</label
      >
      <textarea id="mi-claims" class={inputClass()} rows={3} bind:value={claims}></textarea>
    </div>
    <div>
      <label class="block text-sm font-medium" for="mi-photos"
        >Photographs (JPEG/PNG/WebP, ≤15 MiB each)</label
      >
      <input
        id="mi-photos"
        class="mt-1 block text-sm"
        type="file"
        accept="image/jpeg,image/png,image/webp"
        multiple
        onchange={(e) => (files = e.currentTarget.files)}
      />
    </div>
    {#if error}
      <div class="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800" role="alert">
        {error}
      </div>
    {/if}
    {#if progress}
      <div class="rounded-md border border-slate-200 bg-white p-3 text-sm" role="status">
        {progress}
      </div>
    {/if}
    <button
      class="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
      type="submit"
      disabled={busy}
    >
      {busy ? 'Importing…' : 'Create listing and upload photos'}
    </button>
    {#if doneId}
      <a class="ml-3 text-sm underline" href={`#/listings/${doneId}`}>Open the imported listing →</a
      >
    {/if}
  </form>
</section>
