/**
 * The narrative stream: what the system is saying right now, and what it said before.
 *
 * Contract rules (CLIENT_CONTRACT.md §4), implemented literally:
 *   1. `narrative_start` clears the panel and shows a streaming indicator;
 *   2. `narrative_chunk` text is PROVISIONAL and is appended;
 *   3. `narrative_final` REPLACES the panel — the accumulated chunks are discarded,
 *      because the final text is the guard-checked version;
 *   4. a `template` / `template_fallback` source is badged, not hidden: the safety
 *      facts always came from the rules, and supervisors should see when the LLM
 *      phrasing was rejected.
 *
 * The live region is `polite`: a streaming narrative updates constantly, and an
 * assertive region would make a screen reader unusable.
 */
import { Badge, Panel } from './primitives';
import { formatClock, formatLatency } from '../lib/format';
import type { NarrativeEntry } from '../lib/FatigueSession';
import {
  LLM_STATUS_LABELS,
  isFallbackNarrative,
  llmErrorHint,
  type NarrativeState,
} from '../types';

export interface NarrativePanelProps {
  narrative: NarrativeState;
  log: NarrativeEntry[];
}

/** `dateTime` must be omitted rather than set to "Invalid Date". */
function isoOrUndefined(epochMs: number): string | undefined {
  if (!Number.isFinite(epochMs)) return undefined;
  try {
    return new Date(epochMs).toISOString();
  } catch {
    return undefined;
  }
}

function SourceBadge({ source, streaming }: { source: string | null; streaming: boolean }) {
  if (streaming && source === null) {
    return (
      <Badge tone="warn" title="Text is still arriving and has not been validated">
        provisional
      </Badge>
    );
  }
  if (isFallbackNarrative(source)) {
    return (
      <Badge
        tone="info"
        title="The language model's draft was rejected by the factual guard, or the provider failed; a deterministic template replaced it"
      >
        fallback: {source}
      </Badge>
    );
  }
  if (source === 'llm') {
    return <Badge tone="live">llm</Badge>;
  }
  return <Badge tone="neutral">{source ?? 'unknown source'}</Badge>;
}

/**
 * Which provider wrote this, and whether it is working.
 *
 * Driven entirely by the engine's own account (`provider`, `llm_status`,
 * `error_kind` on each narrative event) rather than by build configuration: a
 * dashboard that claims "Groq" because a flag is set would be asserting something
 * about a request it never saw.
 *
 * The three states are kept visually distinct because they mean different things
 * to the person running the session:
 *   - `ok`         the model wrote the sentence you are reading;
 *   - `skipped`    policy chose not to ask (a CRITICAL verdict never waits on a
 *                  network round trip) — nothing is broken;
 *   - `unavailable` the provider failed. The *risk assessment is still valid* and
 *                  the copy says so, because an LLM outage is not a detection
 *                  outage and must not read like one.
 */
function ProviderLine({ narrative }: { narrative: NarrativeState }) {
  const { provider, llmStatus, errorKind } = narrative;
  if (llmStatus === null && provider === null) return null;

  const label = llmStatus ? LLM_STATUS_LABELS[llmStatus] : 'unknown';
  const hint = llmErrorHint(errorKind);

  return (
    <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-400">
      <span className="text-slate-500">Explanation source:</span>
      <Badge tone={llmStatus === 'ok' ? 'live' : 'neutral'}>
        {provider ?? 'none configured'}
      </Badge>
      <Badge tone={llmStatus === 'ok' ? 'live' : llmStatus === 'unavailable' ? 'warn' : 'info'}>
        {label}
      </Badge>
      {hint ? <span className="text-slate-400">{hint}</span> : null}
    </p>
  );
}

export function NarrativePanel({ narrative, log }: NarrativePanelProps) {
  const hasText = narrative.text.trim().length > 0;

  return (
    <Panel
      title="Narrative stream"
      aside={<SourceBadge source={narrative.source} streaming={narrative.streaming} />}
    >
      <div
        className="min-h-[4.5rem] rounded border border-slate-800 bg-slate-950/60 p-3"
        aria-live="polite"
        aria-atomic="false"
      >
        {hasText ? (
          <p className="text-sm leading-relaxed text-slate-100">
            {narrative.text}
            {narrative.streaming ? <span className="ml-1 animate-pulse text-sky-300">▌</span> : null}
          </p>
        ) : (
          <p className="text-sm text-slate-500">
            {narrative.streaming
              ? 'Generating an explanation…'
              : 'No explanation yet. Narratives are produced when the risk level changes or a trend persists.'}
          </p>
        )}
      </div>

      <ProviderLine narrative={narrative} />

      {narrative.latencyMs !== null ? (
        <p className="mt-2 text-xs text-slate-500">
          Explanation generated in {formatLatency(narrative.latencyMs)}.
        </p>
      ) : null}

      {isFallbackNarrative(narrative.source) ? (
        <p className="mt-2 text-xs text-slate-400">
          A deterministic template was used instead of the language model. The assessment above
          is unchanged — the model only ever supplies phrasing.
        </p>
      ) : null}

      <h3 className="metric-label mb-2 mt-5">Earlier in this session</h3>
      {log.length === 0 ? (
        <p className="text-sm text-slate-500">
          Nothing yet. Only completed explanations are listed here; provisional text is never
          kept as history.
        </p>
      ) : (
        <ol className="max-h-56 space-y-3 overflow-y-auto pr-1">
          {log.map((entry) => (
            <li key={entry.id} className="border-l-2 border-slate-800 pl-3">
              <div className="flex flex-wrap items-center gap-2">
                <time className="font-mono text-xs text-slate-500" dateTime={isoOrUndefined(entry.atMs)}>
                  {formatClock(entry.atMs)}
                </time>
                {entry.riskLevel ? <Badge tone="neutral">{entry.riskLevel}</Badge> : null}
                {isFallbackNarrative(entry.source) ? <Badge tone="info">template</Badge> : null}
              </div>
              <p className="mt-1 text-sm text-slate-300">{entry.text}</p>
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}
