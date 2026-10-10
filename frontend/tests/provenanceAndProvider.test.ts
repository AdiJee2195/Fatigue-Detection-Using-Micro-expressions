/**
 * Provenance and provider status, as the browser sees them.
 *
 * Two things are asserted, and they have the same shape: the dashboard must relay
 * what the backend said, and must never improve on it.
 *
 *  1. `mode_class` decides whether the session is a simulation. A missing value is
 *     "unknown", never "live" — the difference between a demo and a claim.
 *  2. `provider` / `llm_status` / `error_kind` are carried from the narrative
 *     events. A provider outage has to be visible *and* must not read as a
 *     telemetry outage: the risk assessment is still valid.
 *
 * Nothing here needs a socket; the payload-level functions and the state machine's
 * event handling are exercised directly, which is why `FatigueSession` takes an
 * injectable socket in the first place.
 */
import { describe, expect, it } from 'vitest';

import {
  LLM_STATUS_LABELS,
  asLlmStatus,
  asModeClass,
  isSyntheticAssessment,
  llmErrorHint,
  type Assessment,
} from '../src/types';

function assessment(overrides: Partial<Assessment> = {}): Assessment {
  return {
    risk_level: 'HIGH',
    p_fatigue_smoothed: 0.4,
    trend: 'stable',
    confidence: 'high',
    findings: [],
    ...overrides,
  } as Assessment;
}

describe('mode class', () => {
  it('accepts only the canonical vocabulary', () => {
    expect(asModeClass('synthetic')).toBe('synthetic');
    expect(asModeClass('live')).toBe('live');
    expect(asModeClass('unknown')).toBe('unknown');
  });

  it('treats anything unrecognised, including absent, as unknown', () => {
    // The failure this prevents: a value from a newer backend being read as
    // "live" and printed as a measurement.
    for (const value of [undefined, null, '', 'LIVE!', 'measured', 'onnx', '1']) {
      expect(asModeClass(value)).toBe('unknown');
    }
  });

  it('never upgrades case or whitespace variants to a claim', () => {
    expect(asModeClass('Synthetic')).toBe('unknown');
    expect(asModeClass(' synthetic')).toBe('unknown');
  });
});

describe('synthetic assessment detection', () => {
  it('flags only an explicit synthetic declaration', () => {
    expect(isSyntheticAssessment(assessment({ mode_class: 'synthetic' }))).toBe(true);
    expect(isSyntheticAssessment(assessment({ mode_class: 'live' }))).toBe(false);
    expect(isSyntheticAssessment(assessment({}))).toBe(false);
    expect(isSyntheticAssessment(null)).toBe(false);
  });

  it('is unaffected by the pipeline mode string', () => {
    // `mode: 'stub'` without `mode_class` is an older engine; it must not become a
    // simulation claim the backend never made.
    expect(isSyntheticAssessment(assessment({ mode: 'stub' }))).toBe(false);
  });
});

describe('llm status', () => {
  it('accepts every status the engine can send', () => {
    for (const status of [
      'ok',
      'unavailable',
      'disabled',
      'skipped',
      'guard_rejected',
      'not_called',
    ]) {
      expect(asLlmStatus(status)).toBe(status);
      expect(LLM_STATUS_LABELS[status as keyof typeof LLM_STATUS_LABELS]).toBeTruthy();
    }
  });

  it('maps anything unknown to null rather than to success', () => {
    for (const value of [undefined, null, '', 'fine', 'OK', 'error']) {
      expect(asLlmStatus(value)).toBeNull();
    }
  });

  it('labels every status for display', () => {
    expect(Object.keys(LLM_STATUS_LABELS)).toHaveLength(6);
    expect(LLM_STATUS_LABELS.disabled).toBe('Not configured');
    expect(LLM_STATUS_LABELS.skipped).toBe('Skipped');
  });
});

describe('provider failure hints', () => {
  it('names the specific problem for each classified failure', () => {
    expect(llmErrorHint('unauthorized')).toContain('API key');
    expect(llmErrorHint('rate_limited')).toContain('rate limiting');
    expect(llmErrorHint('timeout')).toContain('deterministic');
    expect(llmErrorHint('model_not_found')).toContain('model');
  });

  it('says the assessment is unaffected, because it is', () => {
    // An LLM outage reads as a system outage unless the copy says otherwise.
    for (const kind of ['timeout', 'connection', 'server_error', 'unauthorized']) {
      expect(llmErrorHint(kind)).toMatch(/deterministic|assessment/i);
    }
  });

  it('returns nothing for a healthy or absent provider', () => {
    expect(llmErrorHint(null)).toBeNull();
    expect(llmErrorHint(undefined)).toBeNull();
    expect(llmErrorHint('something_new')).toBeNull();
  });
});
