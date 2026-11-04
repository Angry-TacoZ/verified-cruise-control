import { describe, expect, it } from 'vitest';
import {
  accLabelForState,
  assessAcc,
  isAccAdvertised,
  isAccRequested,
  isAuditInFlight,
  operationalAccMessage,
} from './accVerdict';

describe('isAccAdvertised', () => {
  it('detects adaptive phrasing and brand terms', () => {
    expect(isAccAdvertised(['Adaptive Cruise Control'])).toBe(true);
    expect(isAccAdvertised(['adaptive cruise'])).toBe(true);
    expect(isAccAdvertised(['DISTRONIC'])).toBe(true);
    expect(isAccAdvertised(['Dynamic Radar Cruise Control'])).toBe(true);
    expect(isAccAdvertised(['ProPILOT Assist'])).toBe(true);
  });

  it('never treats generic cruise as ACC', () => {
    expect(isAccAdvertised(['cruise control'])).toBe(false);
    expect(isAccAdvertised(['Cruise Control with SET/RES'])).toBe(false);
    expect(isAccAdvertised([])).toBe(false);
    expect(isAccAdvertised(null)).toBe(false);
  });
});

describe('isAccRequested', () => {
  it('detects the operator check exactly', () => {
    expect(isAccRequested(['adaptive_cruise_control'])).toBe(true);
    expect(isAccRequested(['Adaptive_Cruise_Control'])).toBe(true);
    expect(isAccRequested(['cruise control'])).toBe(false);
    expect(isAccRequested([])).toBe(false);
    expect(isAccRequested(null)).toBe(false);
  });
});

describe('accLabelForState', () => {
  it('maps the five states to PASS / FAIL / INCONCLUSIVE', () => {
    expect(accLabelForState('verified')).toBe('PASS');
    expect(accLabelForState('visually_supported')).toBe('PASS');
    expect(accLabelForState('contradicted')).toBe('FAIL');
    expect(accLabelForState('insufficient_evidence')).toBe('INCONCLUSIVE');
    expect(accLabelForState('not_visually_verifiable')).toBe('INCONCLUSIVE');
  });

  it('resolves unknown or missing states to INCONCLUSIVE, never FAIL', () => {
    expect(accLabelForState('bogus')).toBe('INCONCLUSIVE');
    expect(accLabelForState(null)).toBe('INCONCLUSIVE');
    expect(accLabelForState(undefined)).toBe('INCONCLUSIVE');
  });
});

describe('assessAcc', () => {
  it('marks seller-advertised ACC with a PASS finding', () => {
    const assessment = assessAcc({
      advertisedClaims: ['Adaptive Cruise Control'],
      requestedChecks: [],
      accState: 'visually_supported',
    });
    expect(assessment.label).toBe('PASS');
    expect(assessment.provenance).toBe('seller-advertised');
    expect(assessment.headline).toMatch(/^PASS/);
  });

  it('marks operator-requested ACC without a finding as INCONCLUSIVE', () => {
    const assessment = assessAcc({
      advertisedClaims: ['leather seats'],
      requestedChecks: ['adaptive_cruise_control'],
      accState: null,
    });
    expect(assessment.label).toBe('INCONCLUSIVE');
    expect(assessment.provenance).toBe('operator-requested');
    expect(assessment.limitation).toMatch(/operator-requested/);
    expect(assessment.limitation).toMatch(/does not prove ACC is absent/);
  });

  it('marks contradicted ACC as FAIL', () => {
    const assessment = assessAcc({
      advertisedClaims: ['Adaptive Cruise Control'],
      requestedChecks: [],
      accState: 'contradicted',
    });
    expect(assessment.label).toBe('FAIL');
    expect(assessment.headline).toMatch(/^FAIL/);
  });

  it('never maps insufficient evidence to FAIL', () => {
    const assessment = assessAcc({
      advertisedClaims: ['Adaptive Cruise Control'],
      requestedChecks: [],
      accState: 'insufficient_evidence',
    });
    expect(assessment.label).toBe('INCONCLUSIVE');
  });
});

describe('operational states', () => {
  it('treats queued and running as in-flight', () => {
    expect(isAuditInFlight('queued')).toBe(true);
    expect(isAuditInFlight('running')).toBe(true);
    expect(isAuditInFlight('complete')).toBe(false);
    expect(isAuditInFlight('failed')).toBe(false);
  });

  it('describes failures as non-evidence outcomes', () => {
    expect(operationalAccMessage('failed')).toMatch(/not an ACC evidence result/);
    expect(operationalAccMessage('complete')).toBeNull();
  });
});
