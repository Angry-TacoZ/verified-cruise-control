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

## Running validation (zero inference, always safe)

```sh
python evals/run.py realworld-validate --manifest evals/realworld/manifest.pilot.json
python evals/run.py realworld-validate --manifest <holdout>.json --other-manifest <dev>.json --enforce-freeze --public-report
```

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
