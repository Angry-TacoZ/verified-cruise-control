/**
 * ACC presentation layer (Milestone D).
 *
 * The production five-state evidence taxonomy is preserved everywhere
 * internally. These helpers map it to the three simplified user-facing
 * labels — PASS / INCONCLUSIVE / FAIL — plus operational states that must
 * never be presented as evidence results (queued/running/paused/failed).
 *
 * Critical: absence of an ACC button in photographs never proves ACC is
 * absent, and conventional SET/RES cruise controls never establish ACC.
 * A dedicated following-distance control may visually support ACC without
 * verifying functionality. Unknown or missing states resolve to
 * INCONCLUSIVE, never to FAIL.
 */

export const ACC_CHECK = 'adaptive_cruise_control';

export type AccLabel = 'PASS' | 'INCONCLUSIVE' | 'FAIL';

export type AccProvenance = 'seller-advertised' | 'operator-requested' | 'both' | 'neither';

export interface AccAssessment {
  label: AccLabel;
  /** User-facing headline, e.g. "PASS — Visual evidence supports ACC". */
  headline: string;
  /** Underlying five-state finding, or null when no ACC finding exists. */
  state: string | null;
  provenance: AccProvenance;
  /** False for a completed audit that produced no ACC finding: render the
   * unavailable state, never an evidence-based INCONCLUSIVE badge. */
  available: boolean;
  /** Limitation or ambiguity text shown alongside the result. */
  limitation: string;
}

/**
 * Conservative seller-claim detector. Matches adaptive/dynamic/radar
 * cruise phrasing and known ACC brand terms. Bare "cruise control"
 * (SET/RES) never counts: generic cruise is not ACC evidence.
 */
const ACC_ADVERTISED_PATTERNS = [
  /adaptive[-\s]*cruise/i,
  /\bdynamic\s+radar\s+cruise/i,
  /\bradar\s+cruise/i,
  /\bdrcc\b/i,
  /\bdistronic\b/i,
  /\bpro[-\s]?pilot\b/i,
  /\bintelligent\s+cruise/i,
];

export function isAccAdvertised(claims: string[] | null | undefined): boolean {
  for (const claim of claims ?? []) {
    if (typeof claim !== 'string') continue;
    // Bare cruise-control phrasing without an adaptive qualifier is not ACC.
    if (ACC_ADVERTISED_PATTERNS.some((pattern) => pattern.test(claim))) return true;
  }
  return false;
}

export function isAccRequested(checks: string[] | null | undefined): boolean {
  return (checks ?? []).some(
    (check) => typeof check === 'string' && check.toLowerCase() === ACC_CHECK,
  );
}

/** Map a five-state finding to a user-facing label. Never returns null:
 * unknown or missing states resolve to INCONCLUSIVE with a limitation. */
export function accLabelForState(state: string | null | undefined): AccLabel {
  switch (state) {
    case 'verified':
    case 'visually_supported':
      return 'PASS';
    case 'contradicted':
      return 'FAIL';
    default:
      return 'INCONCLUSIVE';
  }
}

function headlineFor(label: AccLabel, available: boolean): string {
  if (!available) {
    return 'ACC finding unavailable — the completed audit produced no ACC finding';
  }
  switch (label) {
    case 'PASS':
      return 'PASS — Visual evidence supports ACC';
    case 'FAIL':
      return 'FAIL — ACC claim contradicted by authoritative evidence';
    case 'INCONCLUSIVE':
      return 'INCONCLUSIVE — ACC cannot be established from these photos';
  }
}

function limitationFor(
  label: AccLabel,
  state: string | null,
  provenance: AccProvenance,
  available: boolean,
): string {
  if (!available) {
    const scope =
      provenance === 'operator-requested'
        ? 'ACC was operator-requested; the seller did not advertise it. '
        : provenance === 'seller-advertised'
          ? 'ACC was seller-advertised. '
          : '';
    return (
      scope +
      'The audit completed but produced no ACC-specific finding. This is ' +
      'not evidence for or against ACC — absence of a visible control ' +
      'does not prove ACC is absent.'
    );
  }
  if (provenance === 'operator-requested') {
    if (label === 'INCONCLUSIVE' && state === null) {
      return (
        'ACC was operator-requested; the seller did not advertise it. ' +
        'No ACC-specific control is confirmed in the available photos — ' +
        'absence of a visible button does not prove ACC is absent.'
      );
    }
    if (label === 'PASS') {
      return (
        'ACC was operator-requested, not seller-advertised. ' +
        'A dedicated following-distance control visually supports ACC ' +
        'without verifying functionality.'
      );
    }
  }
  if (label === 'INCONCLUSIVE' && state === null) {
    return (
      'No ACC finding was produced for this audit. Conventional SET/RES ' +
      'cruise controls do not establish ACC.'
    );
  }
  if (label === 'INCONCLUSIVE') {
    return (
      'The photos are insufficient, ambiguous, or show no reliable ' +
      'ACC-specific evidence. Generic cruise buttons, radar housings, and ' +
      'unrelated steering-wheel controls are not ACC evidence.'
    );
  }
  if (label === 'FAIL') {
    return 'Reliable evidence conflicts with the advertised ACC feature.';
  }
  return 'An ACC-specific control or indicator is visible in the evidence photos.';
}

export function assessAcc(input: {
  advertisedClaims: string[] | null | undefined;
  requestedChecks: string[] | null | undefined;
  accState: string | null | undefined;
}): AccAssessment {
  const advertised = isAccAdvertised(input.advertisedClaims);
  const requested = isAccRequested(input.requestedChecks);
  const provenance: AccProvenance =
    advertised && requested
      ? 'both'
      : advertised
        ? 'seller-advertised'
        : requested
          ? 'operator-requested'
          : 'neither';
  const state = typeof input.accState === 'string' ? input.accState : null;
  const label = accLabelForState(state);
  const available = state !== null;
  return {
    label,
    headline: headlineFor(label, available),
    state,
    provenance,
    available,
    limitation: limitationFor(label, state, provenance, available),
  };
}

/** Operational audit states that take precedence over any evidence label. */
const IN_FLIGHT = new Set(['queued', 'running']);

export function isAuditInFlight(status: string | null | undefined): boolean {
  return typeof status === 'string' && IN_FLIGHT.has(status);
}

export function operationalAccMessage(status: string | null | undefined): string | null {
  switch (status) {
    case 'queued':
      return 'Audit queued — no ACC result yet.';
    case 'running':
      return 'Audit running — no ACC result yet.';
    case 'paused':
      return 'Audit paused — resume from the listing detail. This is not an ACC result.';
    case 'failed':
      return 'Audit failed (model or pipeline error) — this is not an ACC evidence result.';
    case 'cancelled':
      return 'Audit cancelled before completion. No ACC findings.';
    default:
      return null;
  }
}
