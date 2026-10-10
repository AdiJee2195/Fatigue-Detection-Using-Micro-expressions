/**
 * Reconnect backoff.
 *
 * A monitoring client that hammers a gateway is worse than one that waits: the
 * gateway holds per-session state and every reconnect costs the narrative engine's
 * in-progress narrative (but not the accumulated baseline), so reconnects are
 * deliberately *slow and jittered*.
 *
 * Strategy: exponential base with **full jitter**, floored so a network blip cannot
 * produce a tight loop, and capped at `maxDelayMs` (15 s per the contract).
 */

export interface BackoffOptions {
  baseDelayMs?: number;
  maxDelayMs?: number;
  /** Never return less than this, even after jitter. */
  minDelayMs?: number;
  random?: () => number;
}

export const DEFAULT_BACKOFF: Required<Omit<BackoffOptions, 'random'>> = {
  baseDelayMs: 1_000,
  maxDelayMs: 15_000,
  minDelayMs: 750,
};

/**
 * Delay before reconnect attempt `attempt` (0-based).
 *
 * Full jitter (`random() * ceiling`) is used rather than "exponential + noise"
 * because it is the variant that actually de-correlates many clients reconnecting
 * at once — which is the storm we are trying to avoid.
 */
export function backoffDelay(attempt: number, options: BackoffOptions = {}): number {
  const { baseDelayMs, maxDelayMs, minDelayMs } = { ...DEFAULT_BACKOFF, ...options };
  const random = options.random ?? Math.random;
  const safeAttempt = Number.isFinite(attempt) ? Math.max(0, Math.floor(attempt)) : 0;
  // Cap the exponent before the multiplication so 2 ** 1000 cannot produce Infinity.
  const exponent = Math.min(safeAttempt, 16);
  const ceiling = Math.min(maxDelayMs, baseDelayMs * 2 ** exponent);
  const jittered = random() * ceiling;
  return Math.round(Math.max(minDelayMs, Math.min(maxDelayMs, jittered)));
}
