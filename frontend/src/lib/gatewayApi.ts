/**
 * Gateway monitoring endpoints (`GET /health`, `GET /sessions`).
 *
 * Why the dashboard reads these at all: the browser cannot observe backpressure.
 * Frames are dropped *silently* by design — drop-don't-queue produces no message —
 * so without these endpoints the UI could never show that frames are being dropped,
 * or how much latency the gateway is seeing. That would make the diagnostics panel
 * a guess, and a guessed metric is worse than no metric.
 *
 * Both endpoints read in-memory state only, and the gateway never probes downstream
 * to answer them, so polling is cheap. Still, 5 s is plenty for a diagnostics view.
 */

export interface DownstreamVisionHealth {
  succeeded: number;
  failed: number;
  lastSuccessAt: number | null;
  lastErrorAt: number | null;
  lastErrorReason: string | null;
  /** null means "no attempt observed yet" — NOT healthy. */
  degraded: boolean | null;
}

export interface DownstreamNarrativeHealth {
  connectedSessions: number;
  connects: number;
  reconnects: number;
  lastErrorAt: number | null;
  lastErrorReason: string | null;
  degraded: boolean | null;
}

export interface GatewayHealth {
  status: 'healthy' | 'degraded';
  service?: string;
  version?: string;
  uptimeSeconds?: number;
  sessions?: number;
  framesIngested?: number;
  downstream?: {
    vision?: DownstreamVisionHealth;
    narrative?: DownstreamNarrativeHealth;
    visionUrl?: string;
    narrativeUrl?: string;
  };
}

export interface SessionMetrics {
  sessionId: string;
  framesReceived: number;
  framesProcessed: number;
  framesDropped: number;
  /** -1 means "no data yet" — render as a dash, not as 0 ms. */
  avgLatencyMs: number;
  p95LatencyMs: number;
  effectiveFps: number;
}

/**
 * `ws://host:8080/ws/video/op-1` -> `http://host:8080`
 *
 * Derived rather than configured: a dashboard pointed at a different gateway must
 * not silently poll the default one, which would show another machine's metrics.
 */
export function httpBaseFromWsUrl(wsUrl: string): string {
  const trimmed = wsUrl.trim();
  const match = /^(wss?):\/\/([^/]+)/i.exec(trimmed);
  if (match) {
    const scheme = (match[1] ?? 'ws').toLowerCase() === 'wss' ? 'https' : 'http';
    return `${scheme}://${match[2]}`;
  }
  // Not a ws URL (e.g. a test stub): fall back to the local gateway.
  return 'http://localhost:8080';
}

export const DEFAULT_GATEWAY_WS = 'ws://localhost:8080';

/** Build the session WebSocket URL from a gateway base. */
export function buildSessionUrl(base: string, sessionId: string): string {
  return `${base.replace(/\/+$/, '')}/ws/video/${encodeURIComponent(sessionId)}`;
}

async function fetchJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, headers: { Accept: 'application/json' } });
  if (!response.ok) {
    throw new Error(`${url} responded ${response.status}`);
  }
  return (await response.json()) as T;
}

export interface GatewayDiagnostics {
  health: GatewayHealth | null;
  sessions: SessionMetrics[];
  /** Why the last poll failed, if it did. */
  error: string | null;
  lastPolledAtMs: number | null;
}

export async function fetchDiagnostics(
  baseUrl: string,
  signal: AbortSignal,
): Promise<{ health: GatewayHealth; sessions: SessionMetrics[] }> {
  const base = baseUrl.replace(/\/+$/, '');
  const [health, sessions] = await Promise.all([
    fetchJson<GatewayHealth>(`${base}/health`, signal),
    // A gateway without the sessions endpoint still gives a usable health panel.
    fetchJson<SessionMetrics[]>(`${base}/sessions`, signal).catch(() => [] as SessionMetrics[]),
  ]);
  return { health, sessions };
}

/** Metrics row counts as "no data" when the gateway reports negatives. */
export function hasMetric(value: number | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}
