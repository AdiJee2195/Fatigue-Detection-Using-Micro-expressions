/**
 * Tests for the small pure helpers.
 *
 * The formatting tests are not pedantry: this dashboard renders values that may be
 * null by design (no face, no rPPG provider, no data yet). Printing "NaN" or "0"
 * where the honest answer is "we do not have this number" is a correctness bug in a
 * safety-monitoring context, so the total-ness of these functions is asserted.
 */
import { describe, expect, it } from 'vitest';

import { backoffDelay } from '../src/lib/backoff';
import {
  formatAu,
  formatDuration,
  formatLatency,
  formatNumber,
  formatPercent,
  isFiniteNumber,
  NO_VALUE,
} from '../src/lib/format';
import { buildSessionUrl, httpBaseFromWsUrl } from '../src/lib/gatewayApi';
import { stripDataUrlPrefix } from '../src/lib/frameSource';
import { asSignalStatus, asVisionMode, isFallbackNarrative, unwrapAssessment } from '../src/types';

describe('backoffDelay', () => {
  it('never returns zero, even when the jitter rolls 0', () => {
    for (let attempt = 0; attempt < 12; attempt += 1) {
      const delay = backoffDelay(attempt, { random: () => 0 });
      expect(delay).toBeGreaterThan(0);
    }
  });

  it('never exceeds the 15 s cap', () => {
    for (let attempt = 0; attempt < 40; attempt += 1) {
      const delay = backoffDelay(attempt, { random: () => 1 });
      expect(delay).toBeLessThanOrEqual(15_000);
    }
  });

  it('grows with the attempt count (so a storm cannot tighten)', () => {
    const first = backoffDelay(0, { random: () => 1 });
    const fifth = backoffDelay(5, { random: () => 1 });
    expect(fifth).toBeGreaterThan(first);
  });

  it('survives a non-finite attempt count', () => {
    expect(Number.isFinite(backoffDelay(Number.POSITIVE_INFINITY, { random: () => 1 }))).toBe(true);
    expect(Number.isFinite(backoffDelay(Number.NaN, { random: () => 1 }))).toBe(true);
  });
});

describe('formatters are total', () => {
  const hostile = [null, undefined, Number.NaN, Number.POSITIVE_INFINITY, 'x' as unknown, {}];

  it('returns the placeholder instead of NaN or 0', () => {
    for (const value of hostile) {
      expect(formatNumber(value)).toBe(NO_VALUE);
      expect(formatPercent(value)).toBe(NO_VALUE);
      expect(formatLatency(value)).toBe(NO_VALUE);
      expect(formatDuration(value)).toBe(NO_VALUE);
      expect(formatAu(value)).toBe(NO_VALUE);
      expect(isFiniteNumber(value)).toBe(false);
    }
  });

  it('formats percentages and durations readably', () => {
    expect(formatPercent(0.4231, 1)).toBe('42.3%');
    expect(formatDuration(0)).toBe('0s');
    expect(formatDuration(59_000)).toBe('59s');
    expect(formatDuration(64_000)).toBe('1m 04s');
    expect(formatDuration(3_725_000)).toBe('1h 02m');
    expect(formatLatency(250)).toBe('250 ms');
    expect(formatLatency(2_500)).toBe('2.50 s');
  });
});

describe('gateway URL helpers', () => {
  it('derives the http base from a ws URL, including wss', () => {
    expect(httpBaseFromWsUrl('ws://localhost:8080/ws/video/op-1')).toBe('http://localhost:8080');
    expect(httpBaseFromWsUrl('wss://gateway.example.com/ws/video/op-1')).toBe('https://gateway.example.com');
  });

  it('falls back to the local gateway for an unusable URL', () => {
    expect(httpBaseFromWsUrl('')).toBe('http://localhost:8080');
  });

  it('encodes the session id so a stray character cannot break the path', () => {
    expect(buildSessionUrl('ws://localhost:8080', 'op 1/2')).toBe('ws://localhost:8080/ws/video/op%201%2F2');
    expect(buildSessionUrl('ws://localhost:8080/', 'op-1')).toBe('ws://localhost:8080/ws/video/op-1');
  });
});

describe('frame encoding', () => {
  it('strips a data-URL prefix but leaves a bare payload alone', () => {
    expect(stripDataUrlPrefix('data:image/jpeg;base64,AAAA')).toBe('AAAA');
    expect(stripDataUrlPrefix('AAAA')).toBe('AAAA');
  });
});

describe('contract decoding', () => {
  it('never guesses a signal is live when provenance is missing', () => {
    expect(asSignalStatus('live')).toBe('live');
    expect(asSignalStatus('synthetic')).toBe('synthetic');
    expect(asSignalStatus(undefined)).toBe('unknown');
    expect(asSignalStatus('')).toBe('unknown');
    expect(asSignalStatus('LIVE')).toBe('unknown');
  });

  it('maps the pipeline mode and defaults to unknown', () => {
    expect(asVisionMode('stub')).toBe('stub');
    expect(asVisionMode('onnx')).toBe('onnx');
    expect(asVisionMode(null)).toBe('unknown');
    expect(asVisionMode('something-else')).toBe('unknown');
  });

  it('recognises the guard-rejected narrative sources', () => {
    expect(isFallbackNarrative('template')).toBe(true);
    expect(isFallbackNarrative('template_fallback')).toBe(true);
    expect(isFallbackNarrative('llm')).toBe(false);
    expect(isFallbackNarrative(null)).toBe(false);
  });

  it('accepts both assessment payload shapes and rejects non-objects', () => {
    expect(unwrapAssessment({ assessment: { risk_level: 'HIGH' } })?.risk_level).toBe('HIGH');
    expect(unwrapAssessment({ risk_level: 'LOW' })?.risk_level).toBe('LOW');
    expect(unwrapAssessment(null)).toBeNull();
    expect(unwrapAssessment('nope')).toBeNull();
  });
});
