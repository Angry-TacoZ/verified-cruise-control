# Real-world evaluation dataset (local-only)

This directory holds **versioned manifests** describing real-photograph
evaluation cases. Photographs themselves are **never committed** to the
public repository.

## Layout (planned, Milestone 3)

```
evals/realworld/
├── README.md            # this file
├── cases_v1.json        # case manifests (no image bytes)
└── photos/              # GITIGNORED local overlay (see below)
```

## Manifest schema (per case)

`id`, `photo_sha256`, `photo_file` (local path), `vehicle`
(year/make/model/trim/market), `claim`, `installed_equipment`
(independently established or `null`), `visible_object` (independently
confirmed or `null`), `expected_feature`, `expected_state`,
`ground_truth_method`, `ambiguity`, `limitations`.

## Hygiene rules (binding)

- No dealership photographs, user-provided photographs, copyrighted
  diagrams, VIN-linked records, or private metadata without rights and
  authorization — in the repo, in issues, or in PRs.
- CI runs synthetic or properly licensed fixtures only
  (`fixtures/adas/`, `evals/cases/`).
- The RSA guide is reference material, never ground truth for an
  individual vehicle's equipment.
- `photos/` is gitignored; manifests record SHA-256 so a local photo
  set can be verified without being published.
