/**
 * Current fatigue state: the panel that must answer "how bad is it, and can I trust
 * this?" within a second of looking at it.
 *
 * Everything here comes from the deterministic rule engine's assessment. The UI
 * computes nothing about risk itself — it only formats. That is deliberate: a
 * dashboard that derives its own risk number would be a second, unaudited estimator.
 */
import { Badge, MetricRow, Notice, Panel, RiskChip } from './primitives';
import { formatDuration, formatPercent, formatSince } from '../lib/format';
import { useSinceChange } from '../lib/hooks';
import type { Assessment, ConnectionState, RiskLevel, Telemetry } from '../types';

export interface RiskPanelProps {
  assessment: Assessment | null;
  assessmentAtMs: number | null;
  telemetry: Telemetry | null;
  telemetryAtMs: number | null;
  connection: ConnectionState;
  nowMs: number;
}

function levelOf(assessment: Assessment | null): RiskLevel {
  const level = assessment?.risk_level;
  return level ?? 'UNKNOWN';
}

function trendLabel(trend: Assessment['trend']): string {
  switch (trend) {
    case 'rising':
      return '↑ rising';
    case 'falling':
      return '↓ falling';
    case 'stable':
      return '→ stable';
    default:
      return 'unknown';
  }
}

export function RiskPanel({
  assessment,
  assessmentAtMs,
  telemetry,
  telemetryAtMs,
  connection,
  nowMs,
}: RiskPanelProps) {
  const level = levelOf(assessment);
  const levelSince = useSinceChange(level);

  const hasAssessment = assessment !== null;
  const assessmentAge = assessmentAtMs === null ? null : nowMs - assessmentAtMs;
  // An assessment older than ~10 s means the narrative engine's cadence has stopped.
  // The gateway's contract is roughly every 2 s, so this is well past tolerance.
  const assessmentStale = assessmentAge !== null && assessmentAge > 10_000;

  const smoothed = assessment?.p_fatigue_smoothed;
  const rawRisk = telemetry?.risk_score;

  return (
    <Panel
      title="Current fatigue state"
      aside={
        <>
          {assessment?.trend ? <Badge tone="info">{trendLabel(assessment.trend)}</Badge> : null}
          {assessment?.confidence ? <Badge>confidence {assessment.confidence}</Badge> : null}
        </>
      }
    >
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <RiskChip label="Fatigue state" level={level} detail={assessment?.ts_str ?? undefined} />
        <span className="text-sm text-slate-400" aria-hidden="true">
          {hasAssessment ? `${formatDuration(nowMs - levelSince)} in this state` : ''}
        </span>
      </div>

      {/* The headline case this whole project exists for. */}
      {assessment?.early_drift ? (
        <div className="mb-3">
          <Notice tone="warn" title="Pre-attentive signs detected">
            Eye and facial indicators are drifting while the classifier probability is still
            low. This is the pattern the system is designed to surface early.
          </Notice>
        </div>
      ) : null}

      {assessment?.calibrating ? (
        <div className="mb-3">
          <Notice tone="info">
            Calibrating: the per-user eye-openness baseline is still being learned (about the
            first minute). Treat findings as provisional until it finishes.
          </Notice>
        </div>
      ) : null}

      {level === 'UNKNOWN' && hasAssessment ? (
        <div className="mb-3">
          <Notice tone="warn">
            No usable face data. <strong>UNKNOWN is not the same as safe</strong> — it means the
            system cannot currently assess the operator.
          </Notice>
        </div>
      ) : null}

      {assessmentStale ? (
        <div className="mb-3">
          <Notice tone="warn" title="Assessment is stale">
            The last assessment arrived {formatSince(assessmentAtMs, nowMs)}. The fatigue state
            shown may no longer reflect the operator.
          </Notice>
        </div>
      ) : null}

      <dl className="text-sm">
        <MetricRow
          label="Risk score"
          value={<ValueText value={rawRisk} format={(v) => formatPercent(v, 1)} />}
          source={telemetry ? 'per-frame' : undefined}
          tone="neutral"
        />
        <MetricRow
          label="Fatigue probability (smoothed)"
          value={<ValueText value={smoothed} format={(v) => formatPercent(v, 1)} />}
          source={smoothed === undefined ? undefined : '~5 s EMA'}
        />
        <MetricRow
          label="Confidence"
          value={assessment?.confidence ?? '—'}
          source={assessment?.reliability === false ? 'low reliability' : undefined}
          tone={assessment?.reliability === false ? 'warn' : 'neutral'}
        />
        <MetricRow label="State duration" value={hasAssessment ? formatDuration(nowMs - levelSince) : '—'} />
        <MetricRow
          label="Last assessment"
          value={assessmentAtMs === null ? '—' : formatSince(assessmentAtMs, nowMs)}
        />
        <MetricRow
          label="Last telemetry frame"
          value={telemetryAtMs === null ? '—' : formatSince(telemetryAtMs, nowMs)}
        />
        <MetricRow
          label="System status"
          value={connection}
          source={connection === 'connected' ? 'streaming' : 'interrupted'}
          tone={connection === 'connected' ? 'live' : connection === 'degraded' ? 'warn' : 'error'}
        />
      </dl>

      {assessment?.recommended_action ? (
        <p className="mt-3 border-l-2 border-slate-600 pl-3 text-sm text-slate-300">
          <span className="font-semibold text-slate-200">Suggested action: </span>
          {assessment.recommended_action}
        </p>
      ) : null}

      {assessment?.quality_notes && assessment.quality_notes.length > 0 ? (
        <ul className="mt-3 space-y-1 text-xs text-slate-400">
          {assessment.quality_notes.slice(0, 4).map((note, index) => (
            <li key={`${index}-${note}`}>Data quality: {note}</li>
          ))}
        </ul>
      ) : null}

      {assessment?.signals_available && assessment.signals_available.length > 0 ? (
        <p className="mt-3 text-xs text-slate-500">
          Signals available to the rule engine: {assessment.signals_available.join(', ')}
        </p>
      ) : null}
    </Panel>
  );
}

function ValueText({ value, format }: { value: number | undefined; format: (v: number) => string }) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return <span className="text-slate-500">—</span>;
  }
  return <>{format(value)}</>;
}
