/**
 * Shared presentational primitives.
 *
 * Accessibility rules are enforced here rather than at each call site:
 *   - a status is never conveyed by colour alone: every level ships a word and a
 *     symbol, and `RiskChip` carries a screen-reader sentence;
 *   - panels are real `<section>` landmarks with an accessible name;
 *   - metric rows are definition lists, so a screen reader reads "label, value".
 */
import { useId, type ReactNode } from 'react';

import type { RiskLevel } from '../types';

export interface RiskTone {
  /** Text colour class. */
  text: string;
  /** Background tint class. */
  bg: string;
  /** Border class. */
  border: string;
  /** Non-colour carrier of meaning, shown next to the word. */
  symbol: string;
  /** Plain-language meaning, used for the accessible description. */
  meaning: string;
}

export const RISK_TONE: Record<RiskLevel, RiskTone> = {
  LOW: {
    text: 'text-emerald-300',
    bg: 'bg-emerald-500/10',
    border: 'border-emerald-500/40',
    symbol: '●',
    meaning: 'Indicators are within the normal range for this session.',
  },
  MODERATE: {
    text: 'text-amber-300',
    bg: 'bg-amber-500/10',
    border: 'border-amber-500/40',
    symbol: '▲',
    meaning: 'Some indicators are elevated. Continue monitoring.',
  },
  HIGH: {
    text: 'text-orange-300',
    bg: 'bg-orange-500/10',
    border: 'border-orange-500/40',
    symbol: '▲▲',
    meaning: 'Multiple indicators agree that attention may be degrading.',
  },
  CRITICAL: {
    text: 'text-red-300',
    bg: 'bg-red-500/15',
    border: 'border-red-500/50',
    symbol: '■',
    meaning: 'Strong, corroborated indicators. Immediate attention recommended.',
  },
  UNKNOWN: {
    text: 'text-slate-300',
    bg: 'bg-slate-500/10',
    border: 'border-slate-500/40',
    symbol: '?',
    meaning: 'Not enough usable data to assess. This is not the same as safe.',
  },
};

export type BadgeTone = 'neutral' | 'live' | 'synthetic' | 'warn' | 'error' | 'info';

const BADGE_TONE: Record<BadgeTone, string> = {
  neutral: 'bg-slate-700/60 text-slate-200',
  live: 'bg-emerald-500/20 text-emerald-200',
  synthetic: 'bg-violet-500/20 text-violet-200',
  warn: 'bg-amber-500/20 text-amber-100',
  error: 'bg-red-500/20 text-red-200',
  info: 'bg-sky-500/20 text-sky-200',
};

export function Badge({
  tone = 'neutral',
  children,
  title,
}: {
  tone?: BadgeTone;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`badge ${BADGE_TONE[tone]}`} title={title}>
      {children}
    </span>
  );
}

/**
 * A titled landmark region.
 *
 * `useId` gives every instance a unique name target, so two panels with the same
 * heading cannot produce duplicate ids (which would break `aria-labelledby`).
 */
export function Panel({
  title,
  aside,
  children,
  className = '',
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const id = useId();
  const titleId = `${id}-title`;
  return (
    <section className={`panel ${className}`} aria-labelledby={titleId}>
      <div className="panel-title">
        <h2 id={titleId}>{title}</h2>
        {aside ? <div className="flex items-center gap-2">{aside}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** The risk level as a word + symbol + tint. Never colour-only. */
export function RiskChip({
  label,
  level,
  detail,
}: {
  label: string;
  level: RiskLevel;
  detail?: string;
}) {
  const tone = RISK_TONE[level];
  return (
    <span
      className={`inline-flex items-center gap-2 rounded border px-2 py-1 text-sm font-semibold ${tone.bg} ${tone.border} ${tone.text}`}
      aria-label={`${label}: ${level}. ${tone.meaning}`}
    >
      <span aria-hidden="true">{tone.symbol}</span>
      <span>{level}</span>
      {detail ? <span className="text-xs font-normal opacity-80">{detail}</span> : null}
    </span>
  );
}

export function MetricRow({
  label,
  value,
  source,
  tone = 'neutral',
}: {
  label: string;
  value: ReactNode;
  source?: ReactNode;
  tone?: BadgeTone;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-slate-800/60 py-1.5 last:border-b-0">
      <dt className="metric-label">{label}</dt>
      <dd className="flex items-center gap-2 text-right">
        <span className="metric-value">{value}</span>
        {source ? <Badge tone={tone}>{source}</Badge> : null}
      </dd>
    </div>
  );
}

/** A shaded notice. `tone="error"` upgrades the region to a live alert. */
export function Notice({
  tone,
  children,
  title,
}: {
  tone: 'info' | 'warn' | 'error' | 'synthetic';
  children: ReactNode;
  title?: string;
}) {
  const styles: Record<typeof tone, string> = {
    info: 'border-sky-500/40 bg-sky-500/10 text-sky-100',
    warn: 'border-amber-500/40 bg-amber-500/10 text-amber-100',
    error: 'border-red-500/50 bg-red-500/10 text-red-100',
    synthetic: 'border-violet-500/40 bg-violet-500/10 text-violet-100',
  };
  return (
    <div
      className={`rounded border px-3 py-2 text-sm ${styles[tone]}`}
      role={tone === 'error' ? 'alert' : undefined}
    >
      {title ? <p className="font-semibold">{title}</p> : null}
      <div className={title ? 'mt-1' : ''}>{children}</div>
    </div>
  );
}

/** Render a number with a formatter, or an em dash when we do not have one. */
export function ValueOrDash({ value, format }: { value: unknown; format: (v: number) => string }) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return <span className="text-slate-500">—</span>;
  }
  return <>{format(value)}</>;
}
