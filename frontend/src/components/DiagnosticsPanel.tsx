/**
 * Performance and transport diagnostics.
 *
 * Two independent sources, kept visually separate because they measure different
 * things and must not be conflated:
 *
 *   - **client counters**: how many frames this browser actually sent, and how many
 *     it skipped because the socket was backed up. Only the browser knows this.
 *   - **gateway counters** (`/health`, `/sessions`): how many frames the gateway
 *     received, processed and dropped, and the latency it saw. Only the gateway
 *     knows this.
 *
 * The gap between them is the interesting part: frames skipped client-side never
 * reach the gateway, and frames dropped gateway-side never reach the vision service.
 * Neither number alone tells you whether the pipeline is keeping up.
 */
import { Badge, MetricRow, Notice, Panel } from './primitives';
import { formatDuration, formatLatency, formatNumber, formatSince } from '../lib/format';
import { hasMetric, type GatewayDiagnostics, type SessionMetrics } from '../lib/gatewayApi';

import type { Problem, SessionState } from '../lib/FatigueSession';

export interface DiagnosticsPanelProps {
  state: SessionState;
  diagnostics: GatewayDiagnostics;
  sessionMetrics: SessionMetrics | null;
  measuredSendFps: number | null;
  measuredReceiveFps: number | null;
  nowMs: number;
}

/** `degraded` is null before any traffic is observed — that is "unknown", not "ok". */
function downstreamLabel(degraded: boolean | null | undefined): { text: string; tone: 'live' | 'warn' | 'neutral' } {
  if (degraded === true) return { text: 'degraded', tone: 'warn' };
  if (degraded === false) return { text: 'ok', tone: 'live' };
  return { text: 'unknown (no traffic observed)', tone: 'neutral' };
}

function metricValue(value: number | undefined, format: (v: number) => string): string {
  return hasMetric(value) ? format(value) : '—';
}

export function DiagnosticsPanel({
  state,
  diagnostics,
  sessionMetrics,
  measuredSendFps,
  measuredReceiveFps,
  nowMs,
}: DiagnosticsPanelProps) {
  const { health, sessions, error: diagnosticsError, lastPolledAtMs } = diagnostics;
  const gateway = health?.downstream;
  const dropped = sessionMetrics?.framesDropped;
  const received = sessionMetrics?.framesReceived;

  // Drop ratio is only meaningful once the gateway has counted some frames.
  const dropRatio =
    hasMetric(dropped) && hasMetric(received) && received > 0 ? dropped / received : null;

  return (
    <Panel
      title="Diagnostics"
      aside={
        health ? (
          <Badge tone={health.status === 'healthy' ? 'live' : 'warn'}>
            gateway {health.status}
          </Badge>
        ) : (
          <Badge tone="neutral">gateway unreachable</Badge>
        )
      }
    >
      {diagnosticsError ? (
        <div className="mb-3">
          <Notice tone="warn" title="Monitoring endpoints unavailable">
            {diagnosticsError}
            {lastPolledAtMs === null
              ? ' No reading has ever been successful.'
              : ` Showing age of last reading: ${formatSince(lastPolledAtMs, nowMs)}.`}{' '}
            The WebSocket stream is independent of these endpoints.
          </Notice>
        </div>
      ) : null}

      <h3 className="metric-label mb-2">This browser</h3>
      <dl className="text-sm">
        <MetricRow
          label="Frames sent (this connection)"
          value={state.framesSentSinceConnect.toLocaleString()}
          source={measuredSendFps === null ? undefined : `${formatNumber(measuredSendFps, 1)} fps`}
        />
        <MetricRow
          label="Frames sent (since page load)"
          value={state.framesSent.toLocaleString()}
          source={state.reconnects > 0 ? `across ${state.reconnects + 1} connections` : undefined}
        />
        <MetricRow
          label="Frames skipped (socket busy)"
          value={state.framesSkipped.toLocaleString()}
          tone={state.framesSkipped > 0 ? 'warn' : 'neutral'}
          source={state.framesSkipped > 0 ? 'backpressure' : undefined}
        />
        <MetricRow
          label="Frames unavailable"
          value={state.framesUnavailable.toLocaleString()}
          source="source produced nothing"
        />
        <MetricRow
          label="Telemetry received (this connection)"
          value={state.telemetryReceivedSinceConnect.toLocaleString()}
          source={measuredReceiveFps === null ? undefined : `${formatNumber(measuredReceiveFps, 1)} /s`}
          tone="live"
        />
        <MetricRow
          label="Telemetry received (since page load)"
          value={state.telemetryReceived.toLocaleString()}
        />
        <MetricRow
          label="Telemetry errors"
          value={state.telemetryErrors.toLocaleString()}
          tone={state.telemetryErrors > 0 ? 'warn' : 'neutral'}
        />
        <MetricRow label="Reconnects" value={state.reconnects.toLocaleString()} />
        <MetricRow
          label="Retry in"
          value={state.retryInMs === null ? '—' : formatDuration(state.retryInMs)}
        />
      </dl>

      <h3 className="metric-label mb-2 mt-5">Gateway pipeline (current connection)</h3>
      {!sessionMetrics ? (
        <p className="text-sm text-slate-400">
          The gateway has not listed this session yet. It only appears while the socket is open.
        </p>
      ) : (
        <dl className="text-sm">
          <MetricRow label="Frames received" value={sessionMetrics.framesReceived.toLocaleString()} />
          <MetricRow label="Frames processed" value={sessionMetrics.framesProcessed.toLocaleString()} />
          <MetricRow
            label="Frames dropped"
            value={sessionMetrics.framesDropped.toLocaleString()}
            tone={sessionMetrics.framesDropped > 0 ? 'warn' : 'neutral'}
            source={dropRatio === null ? undefined : `${(dropRatio * 100).toFixed(1)}% of received`}
          />
          <MetricRow
            label="Average vision latency"
            value={metricValue(sessionMetrics.avgLatencyMs, (v) => formatLatency(v))}
          />
          <MetricRow
            label="p95 vision latency"
            value={metricValue(sessionMetrics.p95LatencyMs, (v) => formatLatency(v))}
          />
          <MetricRow
            label="Effective processed rate"
            value={metricValue(sessionMetrics.effectiveFps, (v) => `${formatNumber(v, 1)} fps`)}
          />
        </dl>
      )}

      <h3 className="metric-label mb-2 mt-5">Downstream services (as observed by the gateway)</h3>
      <p className="mb-2 text-xs text-slate-500">
        Derived from real traffic — the gateway does not probe. “Unknown” means nothing has been
        attempted yet, which is not the same as healthy.
      </p>
      {!gateway ? (
        <p className="text-sm text-slate-400">No gateway health reading yet.</p>
      ) : (
        <dl className="text-sm">
          <MetricRow
            label="Vision service"
            value={downstreamLabel(gateway.vision?.degraded).text}
            tone={downstreamLabel(gateway.vision?.degraded).tone}
            source={
              gateway.vision ? `${gateway.vision.succeeded} ok / ${gateway.vision.failed} failed` : undefined
            }
          />
          <MetricRow
            label="Narrative service"
            value={downstreamLabel(gateway.narrative?.degraded).text}
            tone={downstreamLabel(gateway.narrative?.degraded).tone}
            source={
              gateway.narrative
                ? `${gateway.narrative.connectedSessions} connected / ${gateway.narrative.reconnects} reconnects`
                : undefined
            }
          />
          <MetricRow label="Gateway uptime" value={formatDuration((health?.uptimeSeconds ?? 0) * 1000)} />
          <MetricRow label="Active sessions" value={health?.sessions?.toLocaleString() ?? '—'} />
          <MetricRow label="Frames ingested" value={health?.framesIngested?.toLocaleString() ?? '—'} />
          <MetricRow
            label="Last poll"
            value={lastPolledAtMs === null ? '—' : formatSince(lastPolledAtMs, nowMs)}
          />
        </dl>
      )}

      <h3 className="metric-label mb-2 mt-5">Problems</h3>
      {state.problems.length === 0 ? (
        <p className="text-sm text-slate-400">No problems reported this session.</p>
      ) : (
        <ul className="max-h-40 space-y-2 overflow-y-auto pr-1 text-sm">
          {state.problems.map((problem: Problem) => (
            <li key={problem.id} className="flex items-start gap-2">
              <Badge tone={problem.kind === 'connection' ? 'error' : problem.kind === 'telemetry' ? 'warn' : 'info'}>
                {problem.kind}
              </Badge>
              <span className="text-slate-300">
                <span className="font-mono text-xs text-slate-500">{problem.reason}</span> — {problem.message}
              </span>
            </li>
          ))}
        </ul>
      )}

      <p className="mt-3 text-xs text-slate-500">
        {sessions.length > 1
          ? `${sessions.length} sessions are active on this gateway.`
          : 'One session active on this gateway.'}
      </p>
    </Panel>
  );
}
