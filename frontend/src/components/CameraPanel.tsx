/**
 * The frame source, shown honestly.
 *
 * This panel has exactly one job that matters: never imply a camera is running when
 * it is not. Three states are distinguished, and only one of them shows an image:
 *
 *   - a camera is genuinely open  → the real preview, badged `LIVE CAMERA`;
 *   - demo mode                   → no preview at all, badged `SIMULATION`, with the
 *                                   frames described as carrying no image data;
 *   - stopped / unavailable       → no preview, and the reason.
 */
import { useEffect, useRef } from 'react';

import { Badge, MetricRow, Notice, Panel } from './primitives';
import { formatLatency, formatNumber } from '../lib/format';
import type { CameraStatus } from '../lib/frameSource';
import type { ConnectionState, FrameSource, Telemetry, VisionMode } from '../types';

export interface CameraPanelProps {
  stream: MediaStream | null;
  source: FrameSource;
  cameraStatus: CameraStatus;
  cameraMessage: string | null;
  visionMode: VisionMode;
  telemetry: Telemetry | null;
  measuredFps: number | null;
  targetFps: number;
  connection: ConnectionState;
  onStartCamera: () => void;
  onUseDemo: () => void;
}

function faceStateLabel(telemetry: Telemetry | null): string {
  if (!telemetry) return 'no frame processed yet';
  if (telemetry.face_detected === true) return 'face detected';
  if (telemetry.face_detected === false) return 'no face in frame';
  return 'not reported';
}

export function CameraPanel({
  stream,
  source,
  cameraStatus,
  cameraMessage,
  visionMode,
  telemetry,
  measuredFps,
  targetFps,
  connection,
  onStartCamera,
  onUseDemo,
}: CameraPanelProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return undefined;
    video.srcObject = stream;
    if (stream) {
      void video.play().catch(() => undefined);
    }
    return () => {
      video.srcObject = null;
    };
  }, [stream]);

  const live = stream !== null;
  const faceSource = telemetry?.signal_status?.['face_detection'] ?? 'unknown';

  return (
    <Panel
      title="Frame source"
      aside={
        live ? (
          <Badge tone="live">LIVE CAMERA</Badge>
        ) : source === 'demo' ? (
          <Badge tone="synthetic" title="No camera is in use">
            SIMULATION
          </Badge>
        ) : (
          <Badge tone="neutral">NO SOURCE</Badge>
        )
      }
    >
      <div className="relative mb-3 aspect-video w-full overflow-hidden rounded border border-slate-800 bg-slate-950">
        {/* The video element is always mounted so the stream can attach; it is only
            hidden when there is no stream, so an empty box is never mistaken for a
            poor-quality feed. */}
        <video
          ref={videoRef}
          className={`h-full w-full object-cover ${live ? '' : 'hidden'}`}
          autoPlay
          playsInline
          muted
          aria-label="Live camera preview"
        />

        {!live ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 p-4 text-center">
            {source === 'demo' ? (
              <>
                <p className="text-sm font-semibold text-violet-200">Simulation — no camera in use</p>
                <p className="max-w-md text-xs text-slate-400">
                  Frames carry no image data. The vision service is in{' '}
                  <span className="font-mono">stub</span> mode and generates deterministic
                  telemetry without reading any pixels, so no imagery exists to display.
                </p>
              </>
            ) : (
              <>
                <p className="text-sm font-semibold text-slate-300">Camera not in use</p>
                <p className="max-w-md text-xs text-slate-400">
                  {cameraMessage ?? 'Start the camera to stream real frames, or use demo mode.'}
                </p>
              </>
            )}
          </div>
        ) : null}

        {live ? (
          <div className="pointer-events-none absolute left-2 top-2 flex flex-col items-start gap-1">
            <span className="badge bg-black/70 text-emerald-300">
              face: {faceStateLabel(telemetry)}
            </span>
          </div>
        ) : null}
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="rounded border border-slate-600 px-2.5 py-1 text-xs font-semibold text-slate-200 hover:bg-slate-800 disabled:opacity-40"
          onClick={onStartCamera}
          disabled={cameraStatus === 'requesting' || live}
        >
          {live ? 'Camera running' : cameraStatus === 'requesting' ? 'Requesting…' : 'Start camera'}
        </button>
        <button
          type="button"
          className="rounded border border-slate-600 px-2.5 py-1 text-xs font-semibold text-slate-200 hover:bg-slate-800"
          onClick={onUseDemo}
          disabled={source === 'demo' && !live}
        >
          Use demo mode
        </button>
      </div>

      {visionMode === 'stub' ? (
        <div className="mb-3">
          <Notice tone="synthetic" title="DEMO — synthetic telemetry">
            These values are generated by a deterministic demo generator, not measured from a
            person. Nothing below is a real physiological reading.
          </Notice>
        </div>
      ) : null}

      {visionMode === 'onnx' && !live ? (
        <div className="mb-3">
          <Notice tone="warn" title="Real inference, but no camera">
            The vision service is running a real model, yet no camera is streaming frames. With
            no image data there is nothing for the model to assess.
          </Notice>
        </div>
      ) : null}

      {cameraStatus === 'denied' || cameraStatus === 'error' || cameraStatus === 'unavailable' ? (
        <div className="mb-3">
          <Notice tone="warn" title={cameraStatus === 'denied' ? 'Camera permission denied' : 'Camera unavailable'}>
            {cameraMessage ?? 'The camera could not be used.'}{' '}
            <strong>The system will not substitute synthetic imagery for it.</strong>
          </Notice>
        </div>
      ) : null}

      <dl className="text-sm">
        <MetricRow
          label="Source"
          value={live ? 'camera' : source === 'demo' ? 'demo (no image data)' : 'none'}
          source={live ? 'live' : source === 'demo' ? 'simulated' : undefined}
          tone={live ? 'live' : source === 'demo' ? 'synthetic' : 'neutral'}
        />
        <MetricRow
          label="Measured send rate"
          value={measuredFps === null ? '—' : `${formatNumber(measuredFps, 1)} fps`}
          source={
            // Measured, never assumed. When the tab is hidden the browser throttles
            // timers to about once a second, so the achieved rate legitimately falls
            // far below the target — and the panel must say which of the two it is
            // showing rather than leaving a surprisingly low number unexplained.
            measuredFps !== null && measuredFps < targetFps / 2
              ? `target ${targetFps} fps — browser throttling (tab hidden?)`
              : `target ${targetFps} fps`
          }
          tone={measuredFps !== null && measuredFps < targetFps / 2 ? 'warn' : 'neutral'}
        />
        <MetricRow
          label="Vision processing latency"
          value={formatLatency(telemetry?.inference_latency_ms)}
          source={visionMode === 'onnx' ? 'live' : visionMode === 'stub' ? 'simulated' : undefined}
          tone={visionMode === 'onnx' ? 'live' : 'synthetic'}
        />
        <MetricRow label="Face detection source" value={faceSource} />
        <MetricRow label="Stream" value={connection} />
      </dl>
    </Panel>
  );
}
