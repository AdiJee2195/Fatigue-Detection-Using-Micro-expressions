/**
 * Risk over time, for the current session only.
 *
 * Two series: the per-frame `risk_score` and the engine's smoothed fatigue
 * probability. They are shown together on purpose — the smoothed line is what the
 * rule engine reasons about, and seeing the raw line noisier than the smoothed one
 * is the clearest explanation of why hysteresis exists.
 *
 * Memory is bounded by the session's ring buffer (`maxHistory`), so a dashboard left
 * open for hours keeps a fixed-size window rather than growing without limit. The
 * chart therefore covers "the recent window", and the caption says so.
 */
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { ReactNode } from 'react';

import { Badge, Panel } from './primitives';
import { formatPercent } from '../lib/format';
import type { HistoryPoint } from '../lib/FatigueSession';
import type { RiskLevel } from '../types';

export interface RiskTimelineProps {
  history: HistoryPoint[];
  maxHistory: number;
  level: RiskLevel;
}

interface ChartPoint {
  t: number;
  risk: number | null;
  pFatigue: number | null;
  level: RiskLevel | null;
}

function toChartPoints(history: HistoryPoint[]): ChartPoint[] {
  return history.map((point) => ({
    t: Number(point.tSec.toFixed(2)),
    risk: point.risk,
    pFatigue: point.pFatigue,
    level: point.level,
  }));
}

function renderTooltip(props: unknown): ReactNode {
  const { active, payload } = (props ?? {}) as {
    active?: boolean;
    payload?: Array<{ payload?: ChartPoint }>;
  };
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;
  return (
    <div className="rounded border border-slate-700 bg-slate-900 px-3 py-2 text-xs shadow-lg">
      <p className="font-mono text-slate-400">t + {point.t.toFixed(1)} s</p>
      <p className="text-slate-100">
        Risk score: {typeof point.risk === 'number' ? formatPercent(point.risk, 1) : '—'}
      </p>
      <p className="text-slate-100">
        P(fatigue): {typeof point.pFatigue === 'number' ? formatPercent(point.pFatigue, 1) : '—'}
      </p>
      <p className="text-slate-400">Rule-engine level: {point.level ?? 'not reported'}</p>
    </div>
  );
}

export function RiskTimeline({ history, maxHistory, level }: RiskTimelineProps) {
  const data = toChartPoints(history);
  const latest = data.length > 0 ? data[data.length - 1] : undefined;

  return (
    <Panel
      title="Risk timeline"
      aside={
        <>
          <Badge tone="neutral">
            {data.length} / {maxHistory} samples
          </Badge>
          <Badge tone="info">{level}</Badge>
        </>
      }
    >
      {data.length === 0 ? (
        <p className="text-sm text-slate-400">
          No history yet. The timeline fills as telemetry arrives and is capped, so it always
          shows the most recent window rather than the whole session.
        </p>
      ) : (
        <>
          <div
            className="h-56 w-full"
            role="img"
            aria-label={`Risk score and smoothed fatigue probability over the last ${Math.round(
              (latest?.t ?? 0),
            )} seconds. Current rule-engine level ${level}.`}
          >
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: -18 }}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis
                  dataKey="t"
                  type="number"
                  domain={['dataMin', 'dataMax']}
                  tickFormatter={(value: number) => `${value.toFixed(0)}s`}
                  stroke="#64748b"
                  fontSize={11}
                />
                <YAxis
                  domain={[0, 1]}
                  tickFormatter={(value: number) => `${Math.round(value * 100)}%`}
                  stroke="#64748b"
                  fontSize={11}
                />
                <Tooltip content={renderTooltip} />
                {/* The engine's corroboration threshold: where MULTIPLE findings start
                    to matter. Drawn so the crossing is visible, not implied. */}
                <ReferenceLine y={0.5} stroke="#475569" strokeDasharray="4 4" />
                <Line
                  type="monotone"
                  dataKey="risk"
                  name="Risk score"
                  stroke="#38bdf8"
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                  connectNulls
                />
                <Line
                  type="monotone"
                  dataKey="pFatigue"
                  name="P(fatigue) smoothed"
                  stroke="#f59e0b"
                  strokeWidth={1.5}
                  strokeDasharray="4 2"
                  dot={false}
                  isAnimationActive={false}
                  connectNulls
                />
              </LineChart>
            </ResponsiveContainer>
          </div>

          <ul className="mt-3 flex flex-wrap gap-4 text-xs text-slate-400">
            <li className="flex items-center gap-2">
              <span aria-hidden="true" className="inline-block h-0.5 w-4 bg-sky-400" />
              Risk score (per frame)
            </li>
            <li className="flex items-center gap-2">
              <span aria-hidden="true" className="inline-block h-0.5 w-4 border-t border-dashed border-amber-400" />
              P(fatigue), smoothed (~5 s EMA)
            </li>
            <li className="flex items-center gap-2">
              <span aria-hidden="true" className="inline-block h-0.5 w-4 border-t border-dashed border-slate-500" />
              50% reference
            </li>
          </ul>

          <p className="sr-only">
            Latest values: risk score{' '}
            {typeof latest?.risk === 'number' ? formatPercent(latest.risk, 1) : 'not reported'}, smoothed
            fatigue probability{' '}
            {typeof latest?.pFatigue === 'number' ? formatPercent(latest.pFatigue, 1) : 'not reported'}.
          </p>
        </>
      )}
    </Panel>
  );
}
