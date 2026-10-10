/**
 * React adapters for the session state machine and the gateway diagnostics.
 *
 * Both hooks are thin: all of the logic lives in `FatigueSession` and
 * `gatewayApi`, which is what makes the state machine unit-testable without a DOM.
 */
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';

import { FatigueSession, type FatigueSessionOptions, type SessionState } from './FatigueSession';
import {
  type GatewayDiagnostics,
  type GatewayHealth,
  type SessionMetrics,
  fetchDiagnostics,
} from './gatewayApi';

export interface UseFatigueSessionResult {
  state: SessionState;
  session: FatigueSession;
}

/**
 * Owns the session for as long as the component is mounted.
 *
 * The session object is created once per (url, fps) and reused, so switching the
 * frame source or re-rendering never reconnects the socket.
 */
export function useFatigueSession(options: FatigueSessionOptions): UseFatigueSessionResult {
  const { url, fps, getFrame } = options;

  const session = useMemo(
    () =>
      new FatigueSession({
        url,
        fps,
        getFrame,
        staleAfterMs: options.staleAfterMs,
        backoff: options.backoff,
        maxHistory: options.maxHistory,
      }),
    // The frame provider is attached separately via setFrameProvider; re-creating the
    // socket when the camera changes would drop the narrative engine's baseline.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [url, fps],
  );

  useEffect(() => {
    session.start();
    return () => session.dispose();
  }, [session]);

  const state = useSyncExternalStore(session.subscribe, session.getSnapshot, session.getSnapshot);
  return { state, session };
}

export interface UseGatewayDiagnosticsOptions {
  baseUrl: string;
  intervalMs?: number;
  enabled?: boolean;
}

/**
 * Polls `/health` and `/sessions`.
 *
 * Polling stops when the tab is hidden: a monitoring dashboard left open overnight
 * should not keep hitting the gateway, and `document.hidden` is the cheapest honest
 * signal we have for "nobody is looking".
 */
export function useGatewayDiagnostics({
  baseUrl,
  intervalMs = 5_000,
  enabled = true,
}: UseGatewayDiagnosticsOptions): GatewayDiagnostics {
  const [diagnostics, setDiagnostics] = useState<GatewayDiagnostics>({
    health: null,
    sessions: [],
    error: null,
    lastPolledAtMs: null,
  });
  const intervalRef = useRef<number | null>(null);

  useEffect(() => {
    if (!enabled) return undefined;
    const controller = new AbortController();
    let cancelled = false;

    const poll = async () => {
      if (typeof document !== 'undefined' && document.hidden) return;
      try {
        const { health, sessions } = await fetchDiagnostics(baseUrl, controller.signal);
        if (cancelled) return;
        setDiagnostics({ health, sessions, error: null, lastPolledAtMs: Date.now() });
      } catch (error) {
        if (cancelled || controller.signal.aborted) return;
        // A failed poll is reported, never papered over with stale numbers presented
        // as current: the panel shows the error and the age of the last good reading.
        const message = error instanceof Error ? error.message : 'diagnostics unavailable';
        setDiagnostics((previous) => ({ ...previous, error: message }));
      }
    };

    void poll();
    intervalRef.current = window.setInterval(() => void poll(), intervalMs);

    return () => {
      cancelled = true;
      controller.abort();
      if (intervalRef.current !== null) {
        window.clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [baseUrl, intervalMs, enabled]);

  return diagnostics;
}

/** Health of the session currently on screen, or null if the gateway has not listed it. */
export function findSessionMetrics(
  sessions: SessionMetrics[],
  sessionId: string,
): SessionMetrics | null {
  return sessions.find((row) => row.sessionId === sessionId) ?? null;
}

export type { GatewayHealth, SessionMetrics };
