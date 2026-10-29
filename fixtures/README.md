# Synthetic fixtures

Public, reproducible test data for Verified Cruise Control. Everything under
`synthetic-listings/` is generated or hand-written for this repo — no real
dealership content, no unlicensed photos.

- `listing-1987-coupe.html` — synthetic used-car listing with `data-vcc-*`
  extraction attributes (the extractor contract) and ADAS-ish fixture claims.
- `photos/` — Pillow-generated test cards, clearly watermarked SYNTHETIC.
- `../generate.py` — regenerates `photos/` (run from repo root).
- `../serve.py` — serves this directory on `http://127.0.0.1:8901`, the exact
  origin allowlisted in the extension manifest for E2E testing.

## Manual E2E

1. Start the backend, mother app, and fixture server (`scripts/dev.ps1`).
2. Complete operator setup at `http://127.0.0.1:5173/#/setup`.
3. Load the unpacked extension from `apps/extension/.output/chrome-mv3`,
   paste the pairing token into its popup.
4. Open `http://127.0.0.1:8901/listing-1987-coupe.html`, click the extension
   icon, and choose **Import this listing**.
5. Open the listing in the mother app: photos, hashes, and statuses must
   match the fixture; re-importing must not duplicate evidence.
