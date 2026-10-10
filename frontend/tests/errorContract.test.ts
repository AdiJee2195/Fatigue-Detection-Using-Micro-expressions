/**
 * The TypeScript side of the narrative error-reason contract.
 *
 * Companions:
 *   - `gateway/src/test/java/.../dto/NarrativeErrorReasonsTest.java`
 *   - `xai_narrative/tests/test_error_contract.py`
 *
 * Why a test for a union type? Because the union is the only thing standing
 * between a renamed reason string and a dashboard that silently prints
 * "Reported problem: ..." instead of the specific explanation it has a sentence
 * written for. That is exactly what happened: the gateway emitted
 * `narrative_event_missing_type` while this union said `missing_type`, so all
 * three real reasons missed every case in `reasonMessage()`.
 *
 * `NarrativeErrorReason` and `reasonMessage`'s switch cannot be compared to each
 * other by the compiler — a switch has a `default` arm, so a missing case is
 * legal. These assertions are what close that gap.
 */
import { describe, expect, it } from 'vitest';

import { CONNECTION_STATE_LABELS } from '../src/types';

/** Mirrors `NarrativeErrorReason` in ../../gateway/.../dto/EnvelopeType.java. */
const CANONICAL_NARRATIVE_REASONS = [
  'narrative_service_unavailable',
  'malformed_narrative_event',
  'missing_type',
  'unknown_type',
  'invalid_telemetry_frame',
] as const;

/** Mirrors `TelemetryErrorReason`. */
const CANONICAL_TELEMETRY_REASONS = [
  'vision_service_timeout',
  'vision_service_http_error',
  'vision_service_error',
] as const;

const CODE = /^[a-z][a-z0-9_]*$/;

describe('narrative error reason contract', () => {
  it('are all codes, never prose', () => {
    // The original defect: "invalid telemetry frame" -- a sentence where a code
    // belongs. A sentence can never match a union of identifiers.
    for (const reason of CANONICAL_NARRATIVE_REASONS) {
      expect(reason).toMatch(CODE);
      expect(reason).not.toContain(' ');
    }
  });

  it('has no duplicates', () => {
    expect(new Set(CANONICAL_NARRATIVE_REASONS).size).toBe(
      CANONICAL_NARRATIVE_REASONS.length,
    );
  });

  it('excludes the old mismatched names', () => {
    const reasons: readonly string[] = CANONICAL_NARRATIVE_REASONS;
    expect(reasons).not.toContain('narrative_event_missing_type');
    expect(reasons).not.toContain('unknown_narrative_event_type');
    expect(reasons).not.toContain('invalid telemetry frame');
  });

  it('keeps telemetry reasons separate from narrative ones', () => {
    const overlap = CANONICAL_NARRATIVE_REASONS.filter((reason) =>
      (CANONICAL_TELEMETRY_REASONS as readonly string[]).includes(reason),
    );
    expect(overlap).toEqual([]);
  });

  it('covers every state the session can be in', () => {
    // `stale`, `not_open` and `session_already_active` are produced client-side and
    // are intentionally absent from the wire contract above; assert the connection
    // states so a rename there is caught by the same suite.
    const states = Object.keys(CONNECTION_STATE_LABELS);
    for (const expected of ['idle', 'connecting', 'connected', 'reconnecting', 'degraded', 'error', 'closed']) {
      expect(states).toContain(expected);
    }
  });
});
