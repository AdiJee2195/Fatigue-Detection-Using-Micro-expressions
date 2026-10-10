/**
 * Small UI hooks shared by the dashboard panels.
 */
import { useEffect, useRef, useState } from 'react';

/**
 * A ticking clock, in milliseconds.
 *
 * Used for "last update 2s ago" and for state duration. A 1 s tick is chosen on
 * purpose: the app's own timing must not be confused with telemetry timing, and
 * re-rendering every frame just to update a relative timestamp is waste.
 */
export function useNow(intervalMs = 1_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/**
 * Events per second, measured over a short sliding window.
 *
 * `counter` is a monotonically increasing client-side count (frames sent, envelopes
 * received). Sampling it means the displayed rate reflects what actually happened
 * rather than what we asked for — if the browser throttles a background tab, the
 * measured rate drops, and the dashboard must show that instead of the target.
 */
export function useRate(counter: number, windowMs = 4_000, sampleMs = 1_000): number | null {
  const samples = useRef<Array<{ t: number; v: number }>>([]);
  const counterRef = useRef(counter);
  const [rate, setRate] = useState<number | null>(null);

  // Read the latest counter from an interval rather than from the render that
  // changed it: sampling on a timer means a stalled counter correctly decays to 0
  // instead of freezing at its last value.
  useEffect(() => {
    counterRef.current = counter;
  }, [counter]);

  useEffect(() => {
    const sample = () => {
      const now = Date.now();
      const list = samples.current;
      list.push({ t: now, v: counterRef.current });
      let oldest = list[0];
      while (oldest && list.length > 2 && now - oldest.t > windowMs) {
        list.shift();
        oldest = list[0];
      }
      const last = list[list.length - 1];
      if (!oldest || !last || list.length < 2) {
        setRate(null);
        return;
      }
      const elapsedMs = last.t - oldest.t;
      if (elapsedMs <= 0) {
        setRate(null);
        return;
      }
      setRate(Math.max(0, ((last.v - oldest.v) * 1000) / elapsedMs));
    };

    sample();
    const timer = window.setInterval(sample, sampleMs);
    return () => window.clearInterval(timer);
  }, [windowMs, sampleMs]);

  return rate;
}

/**
 * Timestamp at which a value last changed.
 *
 * Used for the fatigue-state duration: "HIGH for 1m 12s" is much more meaningful
 * than an instantaneous level, because the whole point of the system is temporal
 * persistence.
 */
export function useSinceChange<T>(value: T): number {
  const [since, setSince] = useState(() => Date.now());
  const previous = useRef<T>(value);
  useEffect(() => {
    if (previous.current !== value) {
      previous.current = value;
      setSince(Date.now());
    }
  }, [value]);
  return since;
}
