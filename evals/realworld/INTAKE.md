# Real-world pilot intake guide (for James Lane)

This is the human side of PR #5. The coding agent **must not** download
or scrape candidate image bytes, assume public accessibility grants
permission, invent window stickers or build sheets, derive equipment
truth from generic model-year specs, or fabricate photographs, labels,
permissions, or sources. Anything unverified stays `pending`.

## What to supply per candidate case

1. **Original or permission-approved photographs.** Unedited originals
   with intact metadata (the intake records EXIF orientation before
   any transform). Name the file; never paste images into issues or PRs
   without rights clearance.
2. **Usage-rights documentation.** Who took the photo, who holds the
   rights, and what permission was granted (evaluator's own photo,
   written owner/dealership permission, or a license that covers
   evaluation use). A pointer to the record goes in the register
   (`license_evidence`); the document itself stays private.
3. **Vehicle-specific equipment evidence.** Window sticker, build
   sheet, or verified equipment record for THIS vehicle — not a
   brochure, not a model-year spec table, never the RSA guide.
4. **Vehicle configuration.** Make, model, year, trim/package, market,
   plus relevant equipment configuration when established.
5. **The claim being evaluated** and whether it is a **seller claim**
   (preserved verbatim) or a **researcher-authored claim** (never
   presented as a seller claim).

## Checklist before a case is accepted

- [ ] Rights verified (`license_status: verified`) with an evidence pointer
- [ ] Manual privacy review passed (no VIN, plate, owner, address, or
      identifying metadata in the image or its record)
- [ ] Equipment truth independently established with an evidence pointer
- [ ] `source_group` assigned (one opaque id per physical vehicle/listing)
- [ ] Evaluation copy prepared (`prepare_evaluation_copy`), SHA-256 of the
      final copy recorded in the manifest
- [ ] Visible-object and expected-state labels assigned WITHOUT seeing
      any model output; disagreements recorded, not silently resolved
- [ ] Split assigned by source group (never split one vehicle across
      dev and holdout)
- [ ] Reviewer role and attestation recorded in the private register

## Smallest set to complete the pilot

Twelve to twenty accepted cases across 4+ distinct configurations,
prioritizing: ACC vs conventional cruise, ACC Stop & Go, LKA, LDW,
lane-centering, assistance menus, ambiguous symbols, and negative
equipment cases. Prioritize the checklist over category quotas and
report actual coverage. If evidence is insufficient, report the exact
shortfall — do not mark Phase C complete.

## Accepted formats and conversion

Intake accepts JPEG, PNG, and WEBP only (the app's photo format
allowlist). Anything else is refused before any metadata handling:
convert it first with a trusted tool outside the repo, then re-run.
Conversion must not leave GPS or identifying metadata behind — verify
the converted file carries no EXIF GPS, owner, or location tags before
submitting it. HEIC/phone originals always need this step.

## If an original is quarantined for GPS

Do not strip it inside the repo and do not commit it. Strip GPS and
identifying metadata with a trusted tool outside the repo (keeping a
private note of what was removed), confirm the ADAS-relevant region is
visually intact, then re-run intake on the cleaned file. If the
original cannot be cleaned without changing evidence-relevant pixels,
say so and keep the candidate pending.

## Running validation (zero inference, always safe)

```sh
python evals/run.py realworld-validate --manifest evals/realworld/manifest.pilot.json
python evals/run.py realworld-validate --manifest <holdout>.json --other-manifest <dev>.json --enforce-freeze --public-report
```

## Freezing the holdout (before any model evaluation)

The freeze locks the holdout manifest hash and case inventory after
all cross-split gates pass. Run it once the dev/holdout split is
final and BEFORE any model evaluation touches the holdout:

```sh
python evals/run.py realworld-freeze --manifest <holdout>.json \
  --other-manifest <dev>.json --reviewer-role <your-role>
```

The freeze refuses without a dev manifest, full photo-path coverage,
matching SHA-256 digests, a holdout split, and resolved (or
dispositioned) duplicates. Re-running the freeze refuses to overwrite:
to replace a record you must pass `--replace-reason "<why>"`, which
archives the prior record as
`holdout-freeze-<dataset>.superseded-<n>.json` instead of silently
overwriting it. Never evaluate on the holdout before it is frozen, and
never tune prompts against holdout results.

Link the private register with `--register <register.json>` to run
admission. The public branch of the report is counts only; detailed
findings stay in the private directory.

## What stays private, always

Originals and evaluation copies of real photographs, the register,
freeze records, evaluation logs, rights documents, and anything
containing VINs, plates, owner identities, addresses, or private
metadata. All live under `evals/realworld/private/` (or
`$VCC_REALWORLD_PRIVATE_DIR`) and are gitignored; a regression test
fails the suite if those paths ever become committable.
