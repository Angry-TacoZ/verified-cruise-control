# STATE.md — PR #2 Final Evidence Precision

Milestone: fix catalog keyword-collision ranking (Task 1) and SAM
corroboration attribution (Task 2) on `feat/002-multimodal-audit`
(reviewed base `f454a71`). Existing draft PR #2 stays open, unmerged.
No live inference authorized ($0 budget).

## Affected-file map

- `backend/audits/adas_evidence.py` — `match_entry()` ranking:
  specificity (phrase hits, matched chars) first, then vehicle
  applicability, then raw score; full-key ties stay unknown.
- `backend/audits/pipeline.py` — SAM rows record
  `source_observation_id`; v4 support summary claims SAM confirmation
  only for the same observation + control index (legacy rows: same
  claim + photo).
- `backend/tests/test_adas_evidence.py` — collision, wrong-vehicle,
  ambiguity, and valid-match regression tests.
- `backend/tests/test_audits.py` — multi-photo / multi-control SAM
  attribution regression tests.
- `evals/` — goldens re-run; new version only if expectations change.

## Key decisions

- Specificity outranks applicability in match ranking so F1
  rejected-with-entry-id provenance is preserved; the eligibility
  gate remains the no-borrowing backstop. Applicability breaks exact
  specificity ties.
- No catalog keyword changes; claim text never enters matching.
- Corroboration links SAM rows to their source Muse observation id;
  legacy/hand-built rows without the link fall back to same-claim +
  same-photo matching.

## Risks

- Ranking change could alter existing match outcomes: verify full
  backend suite + v2/v4 goldens stay green.
- Summary-text changes could break display assertions: keep legacy
  confirm path byte-identical.

## Acceptance criteria

- [x] Collision, wrong-vehicle, ambiguous, valid-match tests pass.
- [x] Multi-photo / multi-control SAM attribution tests pass.
- [x] Backend suite, Ruff, Django checks, goldens green.
- [x] Mother + extension checks green.
- [ ] Committed + pushed to PR #2; CI reported; PR still draft.

Head `f454a71` verified green locally before this milestone; this
milestone adds specificity-first ranking with applicability tiebreak
(`match_entry` + `vehicle` threading) and same-photo/observation/
control SAM corroboration (`source_observation_id` provenance,
`_sam_corroborates`, same-photo legacy confirm). $0 provider spend.
