/**
 * The fatigue session: one WebSocket per operator session, the connection state
 * machine, the drop-don't-queue frame loop, and the bounded history buffers.
 *
 * Deliberately framework-agnostic (no React import). Two reasons:
 *   1. the state machine is the part most likely to have bugs, and it can only be
 *      tested properly if the socket is injectable — `socketFactory` and `getFrame`
 *      are the seams;
 *   2. `useFatigueSession` stays a thin adapter, so a non-React consumer still works.
 *
 * Contract rules implemented here (see CLIENT_CONTRACT.md):
 *   - reuse the same sessionId across reconnects, exponential backoff capped at 15 s
 *   - never queue frames: if the socket is backed up, skip the frame
 *   - `narrative_chunk` text is PROVISIONAL; `narrative_final` REPLACES the panel
 *   - a `telemetry_error` keeps the socket open (it recovers by itself); only
 *     *silence while sending* is treated as a dead connection
 */

import {
  EMPTY_NARRATIVE,
  asLlmStatus,
  type Assessment,
  type ConnectionState,
  type Envelope,
  type NarrativeFinalPayload,
  type NarrativeStartPayload,
  type NarrativeState,
  type RiskLevel,
  type Telemetry,
  type TelemetryErrorReason,
  unwrapAssessment,
} from '../types';
import { backoffDelay, type BackoffOptions } from './backoff';

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------

export interface HistoryPoint {
  /** Seconds since the session started. Keeps the x-axis small and stable. */
  tSec: number;
  /** `risk_score` from the vision service (mirrors p_fatigue). */
  risk: number | null;
  /** `p_fatigue`. */
  pFatigue: number | null;
  /** Risk level from the most recent assessment, for the tooltip. */
  level: RiskLevel | null;
}

export interface NarrativeEntry {
  id: number;
  atMs: number;
  text: string;
  source: string | null;
  riskLevel: RiskLevel | null;
}

export type ProblemKind = 'connection' | 'telemetry' | 'narrative';

export interface Problem {
  id: number;
  atMs: number;
  kind: ProblemKind;
  reason: string;
  message: string;
}

export interface SessionState {
  connection: ConnectionState;
  /** Why we are degraded. Only 'stale' forces a reconnect. */
  degradedReason: 'error' | 'stale' | null;
  /** Consecutive failed connect attempts (reset on a successful open). */
  attempts: number;
  /** Milliseconds until the next reconnect attempt, or null when not scheduled. */
  retryInMs: number | null;
  reconnects: number;
  lastProblem: string | null;

  sessionStartedAtMs: number;
  connectedSinceMs: number | null;
  lastMessageAtMs: number | null;

  /** Frames sent since the page loaded, across every reconnect. */
  framesSent: number;
  /** Frames not sent because the socket was backed up (drop-don't-queue). */
  framesSkipped: number;
  /** Frames not sent because the source produced nothing usable. */
  framesUnavailable: number;
  /** Telemetry rows received since the page loaded. */
  telemetryReceived: number;
  telemetryErrors: number;
  /**
   * The same two counters, but reset on every successful connect.
   *
   * The gateway's per-session metrics are recreated when a connection is
   * established, so they describe the *current* connection. Comparing them against
   * a cumulative browser counter makes a healthy pipeline look like it is losing
   * frames — the two columns have to cover the same interval to mean anything.
   */
  framesSentSinceConnect: number;
  telemetryReceivedSinceConnect: number;

  telemetry: Telemetry | null;
  telemetryAtMs: number | null;
  assessment: Assessment | null;
  assessmentAtMs: number | null;
  narrative: NarrativeState;
  narrativeLog: NarrativeEntry[];
  problems: Problem[];
  history: HistoryPoint[];
}

export interface FatigueSessionOptions {
  /** Full ws:// URL including the session path. */
  url: string;
  /** Seam for tests: something that behaves like a WebSocket. */
  socketFactory?: (url: string) => WebSocket;
  /**
   * Seam for tests and for the camera layer: returns a base64 JPEG for this tick,
   * or null to skip the tick. Never queue: a null frame is simply not sent.
   */
  getFrame?: () => string | null;
  fps?: number;
  /** Silence (while frames are being sent) after which the link is considered dead. */
  staleAfterMs?: number;
  backoff?: BackoffOptions;
  maxHistory?: number;
  maxNarrativeLog?: number;
  maxProblems?: number;
  now?: () => number;
  /** Max frames per second the UI is notified at. Rendering at 15 Hz is wasteful. */
  notifyThrottleMs?: number;
}

//: Close codes that will not fix themselves. Retrying these is pointless, so the
//: session stops and offers an explicit retry instead of hammering the gateway.
//: 1008 is included, but the duplicate-session case below is handled first: it is
//: the one policy violation that *is* transient.
const PERMANENT_CLOSE_CODES = new Set([1002, 1003, 1008, 1009, 4401, 4403]);

/** `WebSocket.OPEN`. Spelled out so this module loads without a DOM. */
const OPEN = 1;

/** Close code the gateway uses when it refuses a duplicate session id. */
const DUPLICATE_SESSION_CODE = 1008;

/**
 * The gateway's stable close reason for that refusal (`VideoWebSocketHandler`).
 *
 * The code alone cannot carry the meaning: 1008 is a generic policy violation, so
 * the reason string is what identifies this case. An origin rejection never even
 * reaches a close event — the handshake is refused with HTTP 403, which a browser
 * reports as an abnormal 1006 closure — so treating every 1008 as "origin rejected"
 * told the operator to edit the wrong setting when the hold-up was their own
 * previous connection still winding down.
 */
const DUPLICATE_SESSION_REASON = 'session_already_active';

const DEFAULTS = {
  fps: 15,
  staleAfterMs: 8_000,
  maxHistory: 900,
  maxNarrativeLog: 25,
  maxProblems: 20,
  notifyThrottleMs: 100,
};

function reasonMessage(kind: ProblemKind, reason: string): string {
  switch (reason) {
    case 'vision_service_timeout':
      return 'The vision service did not answer in time. Frames are being dropped.';
    case 'vision_service_http_error':
      return 'The vision service returned an error.';
    case 'vision_service_error':
      return 'The vision service failed to process the frame.';
    case 'narrative_service_unavailable':
      return 'Explanations are unavailable. Fatigue telemetry is unaffected.';
    case 'malformed_narrative_event':
      return 'The explanation service sent something unreadable; it was ignored.';
    case 'invalid_telemetry_frame':
      return 'The explanation engine rejected a telemetry frame.';
    case 'session_already_active':
      return 'This session id is still held by the previous connection; reconnecting until the gateway releases it.';
    case 'stale':
      return 'No data received while frames were being sent; reconnecting.';
    case 'not_open':
      return 'The connection closed before the frame could be sent.';
    default:
      return kind === 'connection' ? 'Connection problem.' : `Reported problem: ${reason}`;
  }
}

// ---------------------------------------------------------------------------
// the session
// ---------------------------------------------------------------------------

export class FatigueSession {
  readonly url: string;
  private readonly options: Required<Omit<FatigueSessionOptions, 'socketFactory' | 'getFrame' | 'backoff' | 'now'>>;
  private readonly socketFactory: (url: string) => WebSocket;
  private readonly getFrame: (() => string | null) | null;
  private readonly backoff: BackoffOptions;
  private readonly now: () => number;

  private state: SessionState;
  private listeners = new Set<() => void>();

  private socket: WebSocket | null = null;
  private captureTimer: ReturnType<typeof setTimeout> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private countdownTimer: ReturnType<typeof setInterval> | null = null;
  private watchdogTimer: ReturnType<typeof setInterval> | null = null;
  private notifyTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = true;
  private nextId = 1;
  /** Set true once frames have been sent since the current socket opened. */
  private sendingSinceOpen = false;
  private retryAtMs: number | null = null;
  /** Runtime frame provider (attached by the camera layer after mount). */
  private frameProviderOverride: (() => string | null) | null = null;

  constructor(options: FatigueSessionOptions) {
    this.url = options.url;
    this.options = {
      url: options.url,
      fps: options.fps ?? DEFAULTS.fps,
      staleAfterMs: options.staleAfterMs ?? DEFAULTS.staleAfterMs,
      maxHistory: options.maxHistory ?? DEFAULTS.maxHistory,
      maxNarrativeLog: options.maxNarrativeLog ?? DEFAULTS.maxNarrativeLog,
      maxProblems: options.maxProblems ?? DEFAULTS.maxProblems,
      notifyThrottleMs: options.notifyThrottleMs ?? DEFAULTS.notifyThrottleMs,
    };
    const factory = options.socketFactory;
    if (factory) {
      this.socketFactory = factory;
    } else {
      this.socketFactory = (url: string) => new WebSocket(url);
    }
    this.getFrame = options.getFrame ?? null;
    this.backoff = options.backoff ?? {};
    this.now = options.now ?? (() => Date.now());

    const startedAt = this.now();
    this.state = {
      connection: 'idle',
      degradedReason: null,
      attempts: 0,
      retryInMs: null,
      reconnects: 0,
      lastProblem: null,
      sessionStartedAtMs: startedAt,
      connectedSinceMs: null,
      lastMessageAtMs: null,
      framesSent: 0,
      framesSkipped: 0,
      framesUnavailable: 0,
      telemetryReceived: 0,
      telemetryErrors: 0,
      framesSentSinceConnect: 0,
      telemetryReceivedSinceConnect: 0,
      telemetry: null,
      telemetryAtMs: null,
      assessment: null,
      assessmentAtMs: null,
      narrative: EMPTY_NARRATIVE,
      narrativeLog: [],
      problems: [],
      history: [],
    };
  }

  // ------------------------------------------------------------- subscription
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): SessionState => this.state;

  /**
   * Attach or replace the frame provider. If a socket is already open and no
   * capture loop is running, one starts immediately — the caller does not have to
   * reconnect just because the camera was chosen after mount.
   */
  setFrameProvider(provider: (() => string | null) | null): void {
    this.frameProviderOverride = provider;
    if (provider && this.state.connection === 'connected' && this.captureTimer === null) {
      this.startCapture();
    }
  }

  // ----------------------------------------------------------------- lifecycle
  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.watchdogTimer = setInterval(() => this.checkStale(), 1_000);
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    this.sendingSinceOpen = false;
    if (this.watchdogTimer !== null) {
      clearInterval(this.watchdogTimer);
      this.watchdogTimer = null;
    }
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      // Detach first so the close does not schedule a reconnect.
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      try {
        socket.close(1000, 'client stopped');
      } catch {
        /* already closing */
      }
    }
    this.patch({ connection: 'closed', degradedReason: null, retryInMs: null, connectedSinceMs: null });
  }

  /** Explicit user-initiated retry after a permanent failure. */
  retryNow(): void {
    if (this.stopped) {
      this.start();
      return;
    }
    this.clearTimers();
    this.patch({ attempts: 0, lastProblem: null });
    this.connect();
  }

  private clearTimers(): void {
    if (this.captureTimer !== null) {
      clearTimeout(this.captureTimer);
      this.captureTimer = null;
    }
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    if (this.countdownTimer !== null) {
      clearInterval(this.countdownTimer);
      this.countdownTimer = null;
    }
  }

  // ------------------------------------------------------------------ connect
  private connect(): void {
    if (this.stopped) return;

    let socket: WebSocket;
    try {
      socket = this.socketFactory(this.url);
    } catch {
      // A malformed URL is permanent: retrying it will never work.
      this.failPermanently('the gateway URL is invalid', 'invalid_url');
      return;
    }

    this.socket = socket;
    this.sendingSinceOpen = false;
    this.patch({
      connection: this.state.attempts === 0 ? 'connecting' : 'reconnecting',
      retryInMs: null,
    });

    socket.onopen = () => {
      this.patch({
        connection: 'connected',
        attempts: 0,
        degradedReason: null,
        retryInMs: null,
        connectedSinceMs: this.now(),
        lastMessageAtMs: this.now(),
        // New connection, new comparison window against the gateway's counters.
        framesSentSinceConnect: 0,
        telemetryReceivedSinceConnect: 0,
      });
      this.startCapture();
    };

    socket.onmessage = (event: MessageEvent) => {
      this.handleRawMessage(event.data);
    };

    socket.onerror = () => {
      // onerror is always followed by onclose; record it and let onclose decide.
      this.patch({ lastProblem: 'connection error' });
    };

    socket.onclose = (event: CloseEvent) => {
      this.stopCapture();
      this.socket = null;
      if (this.stopped) return;

      // A duplicate-session rejection is deliberately NOT permanent. The incumbent
      // is usually this page's own previous socket — a React StrictMode remount, a
      // reload, or a fast reconnect — and the gateway releases the id once it has
      // been quiet for `gateway.session-takeover-grace-ms`. Retrying with the normal
      // bounded backoff recovers by itself, instead of parking the operator on an
      // error card they must dismiss.
      if (event.code === DUPLICATE_SESSION_CODE && event.reason === DUPLICATE_SESSION_REASON) {
        this.addProblem('connection', DUPLICATE_SESSION_REASON);
        this.scheduleReconnect();
        return;
      }

      if (PERMANENT_CLOSE_CODES.has(event.code)) {
        this.failPermanently(
          `gateway closed the connection (code ${event.code})`,
          `close_${event.code}`,
        );
        return;
      }

      // Any non-permanent close is retried: a dropped monitoring session must come
      // back on its own, and the backoff cap keeps that from becoming a storm.
      this.scheduleReconnect();
    };
  }

  private failPermanently(message: string, reason: string): void {
    this.stopCapture();
    if (this.socket) {
      const socket = this.socket;
      this.socket = null;
      socket.onclose = null;
      try {
        socket.close();
      } catch {
        /* ignore */
      }
    }
    this.addProblem('connection', reason);
    this.patch({ connection: 'error', retryInMs: null, connectedSinceMs: null, lastProblem: message });
  }

  private scheduleReconnect(): void {
    const delay = backoffDelay(this.state.attempts, this.backoff);
    const attempts = this.state.attempts + 1;
    this.retryAtMs = this.now() + delay;
    this.patch({
      connection: 'reconnecting',
      attempts,
      reconnects: this.state.reconnects + 1,
      retryInMs: delay,
      connectedSinceMs: null,
      degradedReason: null,
    });

    if (this.countdownTimer !== null) clearInterval(this.countdownTimer);
    this.countdownTimer = setInterval(() => {
      if (this.retryAtMs === null) return;
      this.patch({ retryInMs: Math.max(0, this.retryAtMs - this.now()) });
    }, 250);

    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (this.countdownTimer !== null) {
        clearInterval(this.countdownTimer);
        this.countdownTimer = null;
      }
      this.connect();
    }, delay);
  }

  // -------------------------------------------------------------------- frames
  private startCapture(): void {
    this.stopCapture();
    if (!this.resolveFrameProvider()) return;
    const interval = Math.max(1, Math.round(1000 / Math.max(1, this.options.fps)));
    const tick = () => {
      this.sendFrame();
      if (this.stopped) return;
      this.captureTimer = setTimeout(tick, interval);
    };
    this.captureTimer = setTimeout(tick, interval);
  }

  private resolveFrameProvider(): (() => string | null) | null {
    if (this.frameProviderOverride) return this.frameProviderOverride;
    return this.getFrame;
  }

  private stopCapture(): void {
    if (this.captureTimer !== null) {
      clearTimeout(this.captureTimer);
      this.captureTimer = null;
    }
  }

  private sendFrame(): void {
    const socket = this.socket;
    if (!socket || socket.readyState !== OPEN) return;

    // Drop, don't queue. A frame that arrives late is worse than one that never
    // arrives: the whole point of this pipeline is a fresh assessment.
    if (socket.bufferedAmount > 0) {
      this.patch({ framesSkipped: this.state.framesSkipped + 1 });
      return;
    }

    const provider = this.resolveFrameProvider();
    const frame = provider ? provider() : null;
    if (!frame) {
      this.patch({ framesUnavailable: this.state.framesUnavailable + 1 });
      return;
    }

    try {
      socket.send(JSON.stringify({ frame, timestamp: this.now() }));
    } catch {
      // A send that throws means the socket is going away; onclose handles it.
      this.addProblem('connection', 'not_open');
      return;
    }
    this.sendingSinceOpen = true;
    this.patch({
      framesSent: this.state.framesSent + 1,
      framesSentSinceConnect: this.state.framesSentSinceConnect + 1,
    });
  }

  private checkStale(): void {
    const { connection, lastMessageAtMs } = this.state;
    if (connection !== 'connected' && connection !== 'degraded') return;
    if (!this.sendingSinceOpen || lastMessageAtMs === null) return;
    if (this.now() - lastMessageAtMs < this.options.staleAfterMs) return;

    // Silence while we are actively sending means the link is dead even though the
    // socket still looks open. Force a close so the normal reconnect path runs.
    this.addProblem('connection', 'stale');
    this.patch({ connection: 'degraded', degradedReason: 'stale' });
    const socket = this.socket;
    if (socket) {
      try {
        socket.close(4000, 'stale');
      } catch {
        /* ignore */
      }
    }
  }

  // ------------------------------------------------------------------- inbound
  private handleRawMessage(data: unknown): void {
    this.patch({ lastMessageAtMs: this.now() });

    if (typeof data !== 'string') {
      // The gateway always sends text; binary is not part of the contract.
      this.addProblem('connection', 'unexpected_binary_frame');
      return;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      this.addProblem('narrative', 'malformed_envelope');
      return;
    }

    if (!parsed || typeof parsed !== 'object') {
      this.addProblem('narrative', 'malformed_envelope');
      return;
    }

    const envelope = parsed as Envelope<Record<string, unknown>>;
    if (typeof envelope.type !== 'string') {
      this.addProblem('narrative', 'malformed_envelope');
      return;
    }
    this.dispatch(envelope);
  }

  private dispatch(envelope: Envelope<Record<string, unknown>>): void {
    const payload = (envelope.payload ?? {}) as Record<string, unknown>;

    switch (envelope.type) {
      case 'telemetry': {
        const telemetry = payload as Telemetry;
        const atMs = typeof envelope.timestamp === 'number' ? envelope.timestamp : this.now();
        this.pushHistory(telemetry, atMs);
        this.patch({
          telemetry,
          telemetryAtMs: atMs,
          telemetryReceived: this.state.telemetryReceived + 1,
          telemetryReceivedSinceConnect: this.state.telemetryReceivedSinceConnect + 1,
          // Fresh data clears a degraded state caused by an error envelope. It must
          // NOT clear a 'stale' degradation, which is only resolved by reconnecting.
          connection: this.state.connection === 'degraded' && this.state.degradedReason === 'error'
            ? 'connected'
            : this.state.connection,
          degradedReason:
            this.state.degradedReason === 'error' ? null : this.state.degradedReason,
        });
        return;
      }

      case 'telemetry_error': {
        const reason = String(payload['reason'] ?? 'vision_service_error');
        this.addProblem('telemetry', reason);
        this.patch({
          telemetryErrors: this.state.telemetryErrors + 1,
          connection: this.state.connection === 'connected' ? 'degraded' : this.state.connection,
          degradedReason: this.state.degradedReason ?? 'error',
        });
        return;
      }

      case 'assessment': {
        const assessment = unwrapAssessment(payload);
        this.patch({
          assessment,
          assessmentAtMs: typeof envelope.timestamp === 'number' ? envelope.timestamp : this.now(),
        });
        return;
      }

      case 'narrative_start': {
        // Clear the panel and start the streaming indicator. Chunks are provisional.
        // The provider is taken from the event rather than from configuration: the
        // dashboard must not claim Groq is in use because a build flag says so.
        const start = payload as NarrativeStartPayload;
        this.patch({
          narrative: {
            text: '',
            riskLevel: (payload['risk_level'] as RiskLevel) ?? this.state.assessment?.risk_level ?? null,
            source: null,
            streaming: true,
            latencyMs: null,
            hasFinal: this.state.narrative.hasFinal,
            provider: typeof start.provider === 'string' ? start.provider : null,
            llmStatus: asLlmStatus(start.llm_status),
            errorKind: null,
          },
        });
        return;
      }

      case 'narrative_chunk': {
        const text = typeof payload['text'] === 'string' ? payload['text'] : '';
        this.patch({
          narrative: { ...this.state.narrative, streaming: true, text: this.state.narrative.text + text },
        });
        return;
      }

      case 'narrative_retry': {
        // The guard rejected a draft and a silent retry follows; the replacement
        // starts over, so the accumulated provisional text must be discarded.
        this.patch({ narrative: { ...this.state.narrative, text: '', streaming: true } });
        return;
      }

      case 'narrative_final': {
        const final = payload as NarrativeFinalPayload;
        const text = typeof final.text === 'string' ? final.text : '';
        const source = typeof final.source === 'string' ? final.source : null;
        const riskLevel = final.risk_level ?? this.state.assessment?.risk_level ?? null;
        const atMs = typeof envelope.timestamp === 'number' ? envelope.timestamp : this.now();
        // REPLACE, never append: the accumulated chunks are provisional and the
        // final text is the authoritative, guard-checked version.
        this.patch({
          narrative: {
            text,
            source,
            riskLevel,
            streaming: false,
            latencyMs: typeof final.latency_ms === 'number' ? final.latency_ms : null,
            hasFinal: true,
            // Relay the engine's own account of how this text was produced. The
            // badge, the fallback notice and the outage hint all read from here,
            // so none of them can disagree with the engine.
            provider: typeof final.provider === 'string'
              ? final.provider
              : this.state.narrative.provider,
            llmStatus: asLlmStatus(final.llm_status) ?? this.state.narrative.llmStatus,
            errorKind: typeof final.error_kind === 'string' ? final.error_kind : null,
          },
          narrativeLog: this.boundedPrepend(this.state.narrativeLog, {
            id: this.nextId++,
            atMs,
            text,
            source,
            riskLevel,
          }, this.options.maxNarrativeLog),
        });
        return;
      }

      case 'narrative_cancelled': {
        // A more severe alert pre-empted this one: keep the text, stop streaming.
        this.patch({ narrative: { ...this.state.narrative, streaming: false } });
        return;
      }

      case 'narrative_error': {
        const reason = String(payload['reason'] ?? 'narrative_service_unavailable');
        this.addProblem('narrative', reason);
        return;
      }

      default: {
        // Unknown envelope types are ignored rather than treated as an error: the
        // gateway is allowed to add envelope types without breaking old clients.
        return;
      }
    }
  }

  // -------------------------------------------------------------------- history
  private pushHistory(telemetry: Telemetry, atMs: number): void {
    const risk = typeof telemetry.risk_score === 'number' && Number.isFinite(telemetry.risk_score)
      ? telemetry.risk_score
      : null;
    const pFatigue = typeof telemetry.p_fatigue === 'number' && Number.isFinite(telemetry.p_fatigue)
      ? telemetry.p_fatigue
      : null;
    const point: HistoryPoint = {
      tSec: Math.max(0, (atMs - this.state.sessionStartedAtMs) / 1000),
      risk,
      pFatigue,
      level: this.state.assessment?.risk_level ?? null,
    };
    const history = this.state.history;
    // Bounded ring buffer: the timeline must not grow without limit. Dropping the
    // oldest point is correct here because we never claim the chart is more than
    // the recent window.
    if (history.length >= this.options.maxHistory) {
      const overflow = history.length - this.options.maxHistory + 1;
      this.state = { ...this.state, history: [...history.slice(overflow), point] };
      return;
    }
    this.state = { ...this.state, history: [...history, point] };
  }

  private boundedPrepend<T>(list: T[], item: T, max: number): T[] {
    const next = [item, ...list];
    return next.length > max ? next.slice(0, max) : next;
  }

  private addProblem(kind: ProblemKind, reason: string): void {
    const problem: Problem = {
      id: this.nextId++,
      atMs: this.now(),
      kind,
      reason,
      message: reasonMessage(kind, reason),
    };
    this.patch({
      problems: this.boundedPrepend(this.state.problems, problem, this.options.maxProblems),
      lastProblem: problem.message,
    });
  }

  // ------------------------------------------------------------------- patching
  /**
   * Replace the state object (immutably, for `useSyncExternalStore`) and notify.
   *
   * Notifications are throttled: telemetry arrives at up to `fps` per second, and
   * re-rendering the whole dashboard (including a 900-point chart) 15 times a
   * second is wasted work on a laptop. A trailing flush guarantees the last value
   * is never dropped.
   */
  private patch(partial: Partial<SessionState>): void {
    this.state = { ...this.state, ...partial };
    this.scheduleNotify();
  }

  private scheduleNotify(): void {
    if (this.options.notifyThrottleMs <= 0 || this.listeners.size === 0) {
      this.emit();
      return;
    }
    if (this.notifyTimer !== null) return;
    this.notifyTimer = setTimeout(() => {
      this.notifyTimer = null;
      this.emit();
    }, this.options.notifyThrottleMs);
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }

  /** Immediate, unthrottled notification. Used by tests. */
  flush(): void {
    if (this.notifyTimer !== null) {
      clearTimeout(this.notifyTimer);
      this.notifyTimer = null;
    }
    this.emit();
  }

  /** Release everything. Safe to call more than once. */
  dispose(): void {
    this.stop();
    this.listeners.clear();
  }
}

/** Reasons the UI shows for a degraded telemetry path. */
export type { TelemetryErrorReason };
