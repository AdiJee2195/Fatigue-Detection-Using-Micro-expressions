/**
 * The header strip: the three things an operator or supervisor needs before reading
 * any number on the page — is the link alive, which session is this, and is any of
 * this real.
 */
import { useEffect, useState } from 'react';

import { Badge, Notice } from './primitives';
import { formatDuration } from '../lib/format';
import {
  CONNECTION_STATE_LABELS,
  type ConnectionState,
  type FrameSource,
  type VisionMode,
} from '../types';

export interface StatusHeaderProps {
  connection: ConnectionState;
  sessionId: string;
  onSessionIdChange: (next: string) => void;
  visionMode: VisionMode;
  frameSource: FrameSource;
  retryInMs: number | null;
  attempts: number;
  lastProblem: string | null;
  gatewayUrl: string;
  /** True while a real camera stream is open, which the badge must not overstate. */
  cameraLive: boolean;
  onRetry: () => void;
}

function connectionTone(state: ConnectionState): 'live' | 'warn' | 'error' | 'neutral' {
  switch (state) {
    case 'connected':
      return 'live';
    case 'connecting':
    case 'reconnecting':
    case 'degraded':
      return 'warn';
    case 'error':
      return 'error';
    default:
      return 'neutral';
  }
}

function ModeBadge({ mode }: { mode: VisionMode }) {
  if (mode === 'onnx') {
    return (
      <Badge tone="live" title="The vision service is running real model inference">
        LIVE VISION
      </Badge>
    );
  }
  if (mode === 'stub') {
    return (
      <Badge tone="synthetic" title="Synthetic telemetry from the deterministic demo generator">
        DEMO / SYNTHETIC
      </Badge>
    );
  }
  return (
    <Badge tone="neutral" title="The vision service has not reported which pipeline produced the telemetry">
      mode unknown
    </Badge>
  );
}

/** Where the frames come from — separate from which pipeline processed them. */
function SourceBadge({ source, cameraLive }: { source: FrameSource; cameraLive: boolean }) {
  if (cameraLive) {
    return (
      <Badge tone="live" title="Frames are captured from a real camera">
        CAMERA
      </Badge>
    );
  }
  if (source === 'camera') {
    return (
      <Badge tone="warn" title="Camera mode is selected but no frames are available">
        CAMERA (no frames)
      </Badge>
    );
  }
  return (
    <Badge tone="synthetic" title="Frames carry no image data; the pipeline generates telemetry">
      NO IMAGE DATA
    </Badge>
  );
}

export function StatusHeader({
  connection,
  sessionId,
  onSessionIdChange,
  visionMode,
  frameSource,
  retryInMs,
  attempts,
  lastProblem,
  gatewayUrl,
  cameraLive,
  onRetry,
}: StatusHeaderProps) {
  const [draft, setDraft] = useState(sessionId);

  useEffect(() => {
    setDraft(sessionId);
  }, [sessionId]);

  return (
    <header className="border-b border-slate-800 bg-slate-900/70 px-4 py-3 backdrop-blur">
      <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-x-4 gap-y-2">
        <div className="mr-auto">
          <h1 className="text-base font-semibold tracking-tight text-slate-100">
            Operator Fatigue Monitor
          </h1>
          <p className="text-xs text-slate-400">
            Pre-attentive cognitive fatigue detection with explainable risk assessment
          </p>
        </div>

        <ModeBadge mode={visionMode} />
        <SourceBadge source={frameSource} cameraLive={cameraLive} />

        <Badge
          tone={connectionTone(connection)}
          title={`WebSocket to ${gatewayUrl}`}
        >
          {CONNECTION_STATE_LABELS[connection]}
          {connection === 'reconnecting' && retryInMs !== null
            ? ` · retry in ${formatDuration(retryInMs)}`
            : ''}
          {connection === 'reconnecting' && attempts > 1 ? ` · attempt ${attempts}` : ''}
        </Badge>

        <div className="flex items-center gap-2">
          <label className="text-xs uppercase tracking-wide text-slate-400" htmlFor="session-id">
            Session
          </label>
          <input
            id="session-id"
            className="w-40 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-200"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={() => {
              const next = draft.trim();
              if (next && next !== sessionId) onSessionIdChange(next);
              else setDraft(sessionId);
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter') event.currentTarget.blur();
            }}
            aria-describedby="session-id-help"
          />
          <span id="session-id-help" className="sr-only">
            Changing the session id reconnects the WebSocket. The same id must be reused when
            reconnecting so the engine keeps this operator&apos;s baseline.
          </span>
        </div>

        {connection === 'error' ? (
          <button
            type="button"
            onClick={onRetry}
            className="rounded border border-red-500/60 bg-red-500/10 px-2.5 py-1 text-xs font-semibold text-red-100 hover:bg-red-500/20"
          >
            Retry now
          </button>
        ) : null}
      </div>

      {lastProblem && (connection === 'error' || connection === 'degraded') ? (
        <div className="mx-auto mt-2 max-w-[1600px]">
          <Notice tone={connection === 'error' ? 'error' : 'warn'}>{lastProblem}</Notice>
        </div>
      ) : null}
    </header>
  );
}
