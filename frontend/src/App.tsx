/**
 * Application shell.
 *
 * Responsibility split, deliberately:
 *   - `FatigueSession` owns the socket, the state machine and the buffers;
 *   - `CameraController` owns `getUserMedia` and frame capture;
 *   - this component owns *selection and layout* — which source is active, which
 *     panels are shown — and derives nothing about risk.
 *
 * Configuration is read from the URL so a demo can point at any gateway without a
 * rebuild: `?gateway=ws://host:8080&session=operator-042`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { CameraPanel } from './components/CameraPanel';
import { DiagnosticsPanel } from './components/DiagnosticsPanel';
import { Disclaimer } from './components/Disclaimer';
import { ExplainabilityPanel } from './components/ExplainabilityPanel';
import { NarrativePanel } from './components/NarrativePanel';
import { Notice } from './components/primitives';
import { RiskPanel } from './components/RiskPanel';
import { RiskTimeline } from './components/RiskTimeline';
import { StatusHeader } from './components/StatusHeader';
import { TelemetryPanel } from './components/TelemetryPanel';
import {
  CameraController,
  DEFAULT_CAPTURE,
  createPlaceholderFrame,
  type CameraStatus,
} from './lib/frameSource';
import { DEFAULT_GATEWAY_WS, buildSessionUrl, httpBaseFromWsUrl } from './lib/gatewayApi';
import { useNow, useRate } from './lib/hooks';
import { findSessionMetrics, useFatigueSession, useGatewayDiagnostics } from './lib/useFatigueSession';
import { asVisionMode, type FrameSource, type VisionMode } from './types';

/**
 * Target frame rate. The contract suggests 15-24 fps; the vision service is the
 * limiter, and the achieved rate is measured rather than assumed.
 */
const TARGET_FPS = positiveInt(import.meta.env.VITE_CAPTURE_FPS, 15);
const SESSION_STORAGE_KEY = 'fatigue.sessionId';

/** Parse a build-time numeric setting, falling back when it is absent or nonsense. */
function positiveInt(raw: string | undefined, fallback: number): number {
  const value = Number(raw);
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function boundedNumber(raw: string | undefined, fallback: number, min: number, max: number): number {
  const value = Number(raw);
  return Number.isFinite(value) && value >= min && value <= max ? value : fallback;
}

/** Capture settings, overridable at build time; defaults match DEFAULT_CAPTURE. */
const CAPTURE = {
  maxWidth: positiveInt(import.meta.env.VITE_CAPTURE_MAX_WIDTH, DEFAULT_CAPTURE.maxWidth),
  jpegQuality: boundedNumber(import.meta.env.VITE_JPEG_QUALITY, DEFAULT_CAPTURE.jpegQuality, 0.1, 1),
};

interface AppConfig {
  gatewayWs: string;
  sessionId: string;
}

function defaultSessionId(): string {
  return `op-${Math.random().toString(36).slice(2, 8)}`;
}

function readStoredSessionId(): string | null {
  try {
    return window.localStorage.getItem(SESSION_STORAGE_KEY);
  } catch {
    // Private browsing modes can throw on access; a generated id still works.
    return null;
  }
}

/**
 * Configuration precedence, highest first:
 *
 *   1. `?gateway=` / `?session=` — so a demo can point anywhere without a rebuild;
 *   2. the build-time `VITE_*` value — so a container image carries its default;
 *   3. the session id remembered in localStorage — reusing it keeps the engine's
 *      per-operator baseline, which the contract explicitly asks for;
 *   4. a generated id.
 *
 * The session id prefers the remembered value over the build-time default for the
 * same reason: a reload must not silently start a new operator session.
 */
function readConfig(): AppConfig {
  const params = new URLSearchParams(window.location.search);
  const gatewayWs =
    params.get('gateway')?.trim() || import.meta.env.VITE_GATEWAY_WS?.trim() || DEFAULT_GATEWAY_WS;
  const sessionId =
    params.get('session')?.trim() ||
    readStoredSessionId() ||
    import.meta.env.VITE_SESSION_ID?.trim() ||
    defaultSessionId();
  return { gatewayWs, sessionId };
}

export default function App() {
  const initial = useMemo(readConfig, []);
  const [sessionId, setSessionId] = useState(initial.sessionId);
  const [gatewayWs] = useState(initial.gatewayWs);

  // Default to demo mode: requesting a camera on page load would be intrusive, and
  // demo mode is what makes the system demonstrable without one.
  const [source, setSource] = useState<FrameSource>('demo');

  const url = useMemo(() => buildSessionUrl(gatewayWs, sessionId), [gatewayWs, sessionId]);
  const { state, session } = useFatigueSession({ url, fps: TARGET_FPS });

  const nowMs = useNow(1_000);
  const measuredSendFps = useRate(state.framesSent);
  const measuredReceiveFps = useRate(state.telemetryReceived);

  const diagnostics = useGatewayDiagnostics({ baseUrl: httpBaseFromWsUrl(url) });
  const sessionMetrics = findSessionMetrics(diagnostics.sessions, sessionId);

  // ---- session id persistence -------------------------------------------------
  useEffect(() => {
    try {
      window.localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    } catch {
      /* not fatal: the id is in the URL-driven state either way */
    }
  }, [sessionId]);

  // ---- camera ------------------------------------------------------------------
  const [cameraStatus, setCameraStatus] = useState<CameraStatus>('idle');
  const [cameraMessage, setCameraMessage] = useState<string | null>(null);
  const [stream, setStream] = useState<MediaStream | null>(null);
  const controllerRef = useRef<CameraController | null>(null);

  useEffect(() => {
    const controller = new CameraController(() => {
      setCameraStatus(controller.status);
      setCameraMessage(controller.message);
      setStream(controller.getStream());
    });
    controllerRef.current = controller;
    setCameraStatus(controller.status);
    setCameraMessage(controller.message);
    return () => {
      controller.dispose();
      controllerRef.current = null;
    };
  }, []);

  useEffect(() => {
    const controller = controllerRef.current;
    if (!controller) return;
    if (source === 'camera') {
      void controller.start();
    } else {
      controller.stop();
    }
  }, [source]);

  // ---- frame provider ----------------------------------------------------------
  // A blank placeholder with no image content, built once. In demo mode the vision
  // service's stub pipeline ignores frame bytes entirely, so the frame is a
  // transport formality. In ONNX mode a placeholder can never yield a real result,
  // which the panel states outright instead of hiding.
  const placeholder = useMemo(() => createPlaceholderFrame(), []);

  useEffect(() => {
    if (source === 'camera') {
      session.setFrameProvider(() => controllerRef.current?.capture(CAPTURE) ?? null);
    } else if (source === 'demo') {
      session.setFrameProvider(() => placeholder);
    } else {
      session.setFrameProvider(null);
    }
    return () => session.setFrameProvider(null);
  }, [session, source, placeholder]);

  // ---- mode reporting ----------------------------------------------------------
  // Sticky: a single frame without a `mode` field must not flip the banner back to
  // "unknown" and make the operator doubt a correct LIVE VISION badge.
  const [visionMode, setVisionMode] = useState<VisionMode>('unknown');
  useEffect(() => {
    const reported = asVisionMode(state.telemetry?.mode ?? null);
    if (reported !== 'unknown') setVisionMode(reported);
  }, [state.telemetry]);

  const level = state.assessment?.risk_level ?? 'UNKNOWN';

  const handleUseDemo = useCallback(() => setSource('demo'), []);
  const handleStartCamera = useCallback(() => setSource('camera'), []);
  const handleRetry = useCallback(() => session.retryNow(), [session]);

  return (
    <div className="flex min-h-full flex-col">
      <StatusHeader
        connection={state.connection}
        sessionId={sessionId}
        onSessionIdChange={setSessionId}
        visionMode={visionMode}
        frameSource={source}
        retryInMs={state.retryInMs}
        attempts={state.attempts}
        lastProblem={state.lastProblem}
        gatewayUrl={url}
        cameraLive={stream !== null}
        onRetry={handleRetry}
      />

      <main className="mx-auto w-full max-w-[1600px] flex-1 space-y-4 p-4">
        {/* A mismatch between the pipeline mode and the frame source is the single
            most misleading state this UI could accidentally present, so it is
            announced rather than left for the operator to notice. */}
        {visionMode === 'onnx' && source === 'demo' ? (
          <Notice tone="warn" title="Real inference is configured, but frames are synthetic">
            The vision service is running a real model on placeholder frames that contain no
            image. Treat everything below as non-representative until a camera is streaming.
          </Notice>
        ) : null}

        <div className="grid gap-4 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <RiskPanel
              assessment={state.assessment}
              assessmentAtMs={state.assessmentAtMs}
              telemetry={state.telemetry}
              telemetryAtMs={state.telemetryAtMs}
              connection={state.connection}
              nowMs={nowMs}
            />
          </div>
          <CameraPanel
            stream={stream}
            source={source}
            cameraStatus={cameraStatus}
            cameraMessage={cameraMessage}
            visionMode={visionMode}
            telemetry={state.telemetry}
            measuredFps={measuredSendFps}
            targetFps={TARGET_FPS}
            connection={state.connection}
            onStartCamera={handleStartCamera}
            onUseDemo={handleUseDemo}
          />
        </div>

        <RiskTimeline history={state.history} maxHistory={900} level={level} />

        <div className="grid gap-4 lg:grid-cols-2">
          <TelemetryPanel telemetry={state.telemetry} />
          <ExplainabilityPanel assessment={state.assessment} />
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <NarrativePanel narrative={state.narrative} log={state.narrativeLog} />
          <DiagnosticsPanel
            state={state}
            diagnostics={diagnostics}
            sessionMetrics={sessionMetrics}
            measuredSendFps={measuredSendFps}
            measuredReceiveFps={measuredReceiveFps}
            nowMs={nowMs}
          />
        </div>
      </main>

      <Disclaimer />
    </div>
  );
}
