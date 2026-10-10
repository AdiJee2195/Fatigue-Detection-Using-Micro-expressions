/**
 * Formatting helpers.
 *
 * Every function here must be total: given null, undefined, NaN or an absurd value
 * it returns a placeholder rather than "NaN" or "undefined". A monitoring UI that
 * prints NaN is worse than one that admits it does not have the number, because
 * NaN looks like a rendering bug rather than a missing measurement.
 */

/** Placeholder for a value we do not have. Deliberately not "0" or "-". */
export const NO_VALUE = '—';

export function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

/** Fixed-decimal number, or the placeholder. */
export function formatNumber(value: unknown, digits = 2): string {
  if (!isFiniteNumber(value)) return NO_VALUE;
  return value.toFixed(digits);
}

/** 0-1 value as a whole-number percentage. */
export function formatPercent(value: unknown, digits = 0): string {
  if (!isFiniteNumber(value)) return NO_VALUE;
  return `${(value * 100).toFixed(digits)}%`;
}

/** Milliseconds, switching to seconds above 1000 ms. */
export function formatLatency(value: unknown): string {
  if (!isFiniteNumber(value)) return NO_VALUE;
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`;
  return `${value.toFixed(0)} ms`;
}

/** Clock time (HH:MM:SS) from an epoch-millis timestamp. */
export function formatClock(epochMs: unknown): string {
  if (!isFiniteNumber(epochMs)) return NO_VALUE;
  try {
    return new Date(epochMs).toLocaleTimeString(undefined, { hour12: false });
  } catch {
    return NO_VALUE;
  }
}

/**
 * Elapsed duration, compact: `3s`, `1m 04s`, `1h 02m`.
 * Used for "state duration" and "session uptime", so it must stay short.
 */
export function formatDuration(ms: unknown): string {
  if (!isFiniteNumber(ms) || ms < 0) return NO_VALUE;
  const totalSeconds = Math.floor(ms / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`;
}

/** "+2.3s ago" style relative time, for "last update". */
export function formatSince(epochMs: unknown, nowMs: number): string {
  if (!isFiniteNumber(epochMs)) return NO_VALUE;
  const delta = Math.max(0, nowMs - epochMs);
  if (delta < 1_000) return 'just now';
  return `${formatDuration(delta)} ago`;
}

/** FACS AU intensity (0-5) rendered on its own scale. */
export function formatAu(value: unknown): string {
  if (!isFiniteNumber(value)) return NO_VALUE;
  return value.toFixed(2);
}

/** Short, human-readable explanation of a provenance value. */
export function describeSource(source: string): string {
  switch (source) {
    case 'mediapipe':
      return 'MediaPipe';
    case 'opencv':
      return 'OpenCV Haar';
    case 'centre_crop':
      return 'Centre crop (no face found)';
    case 'synthetic':
      return 'Synthetic (demo)';
    case 'live':
      return 'Live';
    case 'none':
      return 'None';
    default:
      return source;
  }
}

/** Sort risk levels by severity for colouring and comparisons. */
export const RISK_ORDER: Record<string, number> = {
  UNKNOWN: -1,
  LOW: 0,
  MODERATE: 1,
  HIGH: 2,
  CRITICAL: 3,
};
