/**
 * State machine, transport and narrative tests for `FatigueSession`.
 *
 * A fake socket is injected, so no network, no browser and no gateway are involved.
 * The behaviours covered here are the ones where a bug is silent and dangerous: a
 * dropped frame that is never counted, a provisional narrative kept as if final, a
 * reconnect storm, or a dead link that still looks connected.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FatigueSession } from '../src/lib/FatigueSession';

/** Minimal stand-in for `WebSocket`, tracking what the session actually did. */
class FakeSocket {
  static instances: FakeSocket[] = [];

  readyState = 0; // CONNECTING
  bufferedAmount = 0;
  sent: string[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: { code: number; reason?: string }) => void) | null = null;

  constructor(public readonly url: string) {
    FakeSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(code = 1000): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }

  // ---- test helpers ----
  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  /** Deliver a message the way the browser does: a JSON string. */
  deliver(payload: unknown): void {
    this.onmessage?.({ data: typeof payload === 'string' ? payload : JSON.stringify(payload) });
  }

  serverClose(code = 1006, reason = ''): void {
    this.readyState = 3;
    this.onclose?.({ code, reason });
  }

  get sentFrames(): Array<{ frame: string; timestamp: number }> {
    return this.sent.map((raw) => JSON.parse(raw) as { frame: string; timestamp: number });
  }
}

function envelope(type: string, payload: unknown, timestamp = Date.now()): string {
  return JSON.stringify({ type, sessionId: 'op-1', payload, timestamp });
}

function makeSession(overrides: Partial<ConstructorParameters<typeof FatigueSession>[0]> = {}) {
  return new FatigueSession({
    url: 'ws://test/ws/video/op-1',
    socketFactory: (url) => new FakeSocket(url) as unknown as WebSocket,
    getFrame: () => 'FRAME',
    fps: 10,
    notifyThrottleMs: 0,
    ...overrides,
  });
}

function latest(): FakeSocket {
  const socket = FakeSocket.instances[FakeSocket.instances.length - 1];
  if (!socket) throw new Error('no socket was created');
  return socket;
}

beforeEach(() => {
  FakeSocket.instances = [];
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
});

afterEach(() => {
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// connection lifecycle
// ---------------------------------------------------------------------------
describe('connection state machine', () => {
  it('is idle before start and connects on start', () => {
    const session = makeSession();
    expect(session.getSnapshot().connection).toBe('idle');

    session.start();
    expect(session.getSnapshot().connection).toBe('connecting');

    latest().open();
    expect(session.getSnapshot().connection).toBe('connected');
    expect(session.getSnapshot().attempts).toBe(0);
    session.dispose();
  });

  it('reports the real URL to the socket', () => {
    const session = makeSession();
    session.start();
    expect(latest().url).toBe('ws://test/ws/video/op-1');
    session.dispose();
  });

  it('reconnects after an unexpected drop and resets attempts on success', () => {
    const session = makeSession();
    session.start();
    latest().open();

    latest().serverClose();
    expect(session.getSnapshot().connection).toBe('reconnecting');
    expect(session.getSnapshot().attempts).toBe(1);
    expect(session.getSnapshot().reconnects).toBe(1);
    expect(session.getSnapshot().retryInMs).toBeGreaterThan(0);

    vi.advanceTimersByTime(16_000);
    expect(FakeSocket.instances.length).toBeGreaterThanOrEqual(2);

    latest().open();
    expect(session.getSnapshot().connection).toBe('connected');
    expect(session.getSnapshot().attempts).toBe(0);
    session.dispose();
  });

  it('never reconnects once stopped', () => {
    const session = makeSession();
    session.start();
    latest().open();
    const socketCount = FakeSocket.instances.length;

    session.stop();
    expect(session.getSnapshot().connection).toBe('closed');

    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances.length).toBe(socketCount);
  });

  it('treats an unnamed policy-violation close as permanent instead of retrying', () => {
    const session = makeSession();
    session.start();
    // 1008 is retryable only when the gateway names the duplicate-session reason
    // (see the next test); any other policy violation is a decision we cannot fix
    // by reconnecting.
    latest().serverClose(1008, 'something_else');

    expect(session.getSnapshot().connection).toBe('error');
    expect(session.getSnapshot().lastProblem).toContain('1008');

    // Backoff timers must not resurrect it.
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances.length).toBe(1);
    session.dispose();
  });

  it('retries a duplicate-session rejection and recovers by itself', () => {
    // React StrictMode mounts effects twice in development: the cleanup closes the
    // first socket and the second attempt can reach the gateway before that close
    // has been processed. The refusal is transient by construction — the gateway
    // releases the id once it has been quiet — so the session must reconnect
    // rather than park the operator on an error card.
    const session = makeSession();
    session.start();
    latest().open();

    latest().serverClose(1008, 'session_already_active');

    const state = session.getSnapshot();
    expect(state.connection).toBe('reconnecting');
    expect(state.problems.some((problem) => problem.reason === 'session_already_active')).toBe(true);
    expect(state.lastProblem).toContain('still held by the previous connection');

    vi.advanceTimersByTime(16_000);
    expect(FakeSocket.instances.length).toBe(2);
    latest().open();
    expect(session.getSnapshot().connection).toBe('connected');
    session.dispose();
  });

  it('reconnects on an explicit retry after a permanent failure', () => {
    const session = makeSession();
    session.start();
    latest().serverClose(1008);
    expect(session.getSnapshot().connection).toBe('error');

    session.retryNow();
    expect(FakeSocket.instances.length).toBe(2);
    expect(session.getSnapshot().connection).toBe('connecting');

    latest().open();
    expect(session.getSnapshot().connection).toBe('connected');
    session.dispose();
  });

  it('treats an invalid URL as a permanent error', () => {
    const session = makeSession({
      socketFactory: () => {
        throw new Error('bad url');
      },
    });
    session.start();
    expect(session.getSnapshot().connection).toBe('error');
    session.dispose();
  });
});

// ---------------------------------------------------------------------------
// frame loop and backpressure
// ---------------------------------------------------------------------------
describe('frame loop', () => {
  it('sends frames at the configured rate', () => {
    const session = makeSession({ fps: 10 });
    session.start();
    latest().open();

    vi.advanceTimersByTime(1_000);
    expect(latest().sentFrames.length).toBe(10);
    expect(session.getSnapshot().framesSent).toBe(10);
    session.dispose();
  });

  it('drops frames instead of queueing when the socket is backed up', () => {
    const session = makeSession({ fps: 10 });
    session.start();
    latest().open();

    vi.advanceTimersByTime(500);
    const sentBefore = latest().sentFrames.length;

    latest().bufferedAmount = 4096;
    vi.advanceTimersByTime(500);

    // The backed-up window must produce no sends at all, and every skipped tick must
    // be counted — a silent drop would make the diagnostics panel lie.
    expect(latest().sentFrames.length).toBe(sentBefore);
    expect(session.getSnapshot().framesSkipped).toBeGreaterThanOrEqual(4);
    session.dispose();
  });

  it('counts frames the source could not produce without sending them', () => {
    const session = makeSession({ getFrame: () => null, fps: 10 });
    session.start();
    latest().open();

    vi.advanceTimersByTime(1_000);
    expect(latest().sentFrames.length).toBe(0);
    expect(session.getSnapshot().framesUnavailable).toBe(10);
    session.dispose();
  });

  it('stops sending on the closed socket immediately', () => {
    const session = makeSession({ fps: 10 });
    session.start();
    const socket = latest();
    socket.open();
    vi.advanceTimersByTime(500);

    const sentBeforeClose = socket.sentFrames.length;
    expect(sentBeforeClose).toBeGreaterThan(0);
    socket.serverClose();

    // The capture loop must not keep writing to a dead socket while the backoff
    // delay is pending; a reconnect creates a fresh socket instead.
    vi.advanceTimersByTime(500);
    expect(socket.sentFrames.length).toBe(sentBeforeClose);
    session.dispose();
  });

  it('resets the per-connection counters on reconnect but keeps the lifetime totals', () => {
    const session = makeSession({ fps: 10 });
    session.start();
    latest().open();
    vi.advanceTimersByTime(1_000);
    latest().deliver(envelope('telemetry', { risk_score: 0.3 }));

    expect(session.getSnapshot().framesSentSinceConnect).toBe(10);
    expect(session.getSnapshot().telemetryReceivedSinceConnect).toBe(1);

    latest().serverClose();
    vi.advanceTimersByTime(16_000);
    latest().open();

    // The gateway recreates its per-session metrics on reconnect, so the browser's
    // comparison window must restart too — otherwise a healthy pipeline looks lossy.
    const afterReconnect = session.getSnapshot();
    expect(afterReconnect.framesSentSinceConnect).toBe(0);
    expect(afterReconnect.telemetryReceivedSinceConnect).toBe(0);
    expect(afterReconnect.framesSent).toBeGreaterThan(0);
    expect(afterReconnect.telemetryReceived).toBe(1);
    session.dispose();
  });

  it('stamps every frame with a millisecond timestamp', () => {
    const session = makeSession({ fps: 10 });
    session.start();
    latest().open();
    vi.advanceTimersByTime(200);

    const frames = latest().sentFrames;
    expect(frames.length).toBeGreaterThan(0);
    const first = frames[0];
    expect(first).toBeDefined();
    expect(first?.frame).toBe('FRAME');
    // Epoch millis, not seconds: the gateway converts and normalises it.
    expect(first?.timestamp).toBeGreaterThan(1e12);
    session.dispose();
  });
});

// ---------------------------------------------------------------------------
// stale detection
// ---------------------------------------------------------------------------
describe('stale connection detection', () => {
  it('reconnects when frames are being sent but nothing comes back', () => {
    const session = makeSession({ fps: 5, staleAfterMs: 3_000 });
    session.start();
    latest().open();

    // Frames flow, the socket stays open, and the server never answers: that is a
    // dead link, not a healthy quiet one.
    vi.advanceTimersByTime(4_000);
    expect(session.getSnapshot().problems.some((p) => p.reason === 'stale')).toBe(true);
    expect(session.getSnapshot().connection).toBe('reconnecting');

    vi.advanceTimersByTime(20_000);
    expect(FakeSocket.instances.length).toBeGreaterThanOrEqual(2);
    session.dispose();
  });

  it('does not call a quiet connection stale while no frames are being sent', () => {
    // No frame source: silence is expected, so declaring the link dead would be
    // wrong — and would produce a reconnect loop for a user who is simply idle.
    const session = makeSession({ fps: 5, staleAfterMs: 2_000, getFrame: () => null });
    session.start();
    latest().open();

    vi.advanceTimersByTime(10_000);
    expect(session.getSnapshot().connection).toBe('connected');
    expect(FakeSocket.instances.length).toBe(1);
    session.dispose();
  });
});

// ---------------------------------------------------------------------------
// envelope dispatch
// ---------------------------------------------------------------------------
describe('envelope handling', () => {
  it('stores telemetry', () => {
    const session = makeSession();
    session.start();
    latest().open();

    latest().deliver(envelope('telemetry', { risk_score: 0.42, p_fatigue: 0.5, mode: 'stub' }));

    expect(session.getSnapshot().telemetry?.risk_score).toBe(0.42);
    expect(session.getSnapshot().telemetryReceived).toBe(1);
    expect(session.getSnapshot().telemetryAtMs).not.toBeNull();
    session.dispose();
  });

  it('ignores malformed JSON and unknown envelope types without breaking', () => {
    const session = makeSession();
    session.start();
    latest().open();

    latest().deliver('{not json');
    latest().deliver(envelope('something_new', { anything: true }));
    latest().deliver(envelope('telemetry', { risk_score: 0.1 }));

    // The good frame after the bad ones must still land.
    expect(session.getSnapshot().telemetryReceived).toBe(1);
    expect(session.getSnapshot().problems.some((p) => p.reason === 'malformed_envelope')).toBe(true);
    session.dispose();
  });

  it('surfaces a telemetry error and recovers on the next frame without reconnecting', () => {
    const session = makeSession();
    session.start();
    latest().open();

    latest().deliver(envelope('telemetry_error', { reason: 'vision_service_timeout' }));
    expect(session.getSnapshot().connection).toBe('degraded');
    expect(session.getSnapshot().degradedReason).toBe('error');
    expect(session.getSnapshot().telemetryErrors).toBe(1);

    latest().deliver(envelope('telemetry', { risk_score: 0.2 }));
    expect(session.getSnapshot().connection).toBe('connected');
    expect(session.getSnapshot().degradedReason).toBeNull();
    // The contract says the socket stays open and recovers by itself.
    expect(FakeSocket.instances.length).toBe(1);
    session.dispose();
  });

  it('unwraps an assessment from either payload shape', () => {
    const session = makeSession();
    session.start();
    latest().open();

    latest().deliver(envelope('assessment', { assessment: { risk_level: 'HIGH', findings: [] } }));
    expect(session.getSnapshot().assessment?.risk_level).toBe('HIGH');

    latest().deliver(envelope('assessment', { risk_level: 'LOW', findings: [] }));
    expect(session.getSnapshot().assessment?.risk_level).toBe('LOW');
    session.dispose();
  });

  it('keeps the problem list bounded', () => {
    const session = makeSession({ maxProblems: 3 });
    session.start();
    latest().open();

    for (let i = 0; i < 10; i += 1) {
      latest().deliver(envelope('telemetry_error', { reason: `reason_${i}` }));
    }
    expect(session.getSnapshot().problems.length).toBe(3);
    expect(session.getSnapshot().problems[0]?.reason).toBe('reason_9');
    session.dispose();
  });
});

// ---------------------------------------------------------------------------
// narrative rules (CLIENT_CONTRACT.md §4)
// ---------------------------------------------------------------------------
describe('narrative panel rules', () => {
  it('clears the panel on start and appends chunks as provisional', () => {
    const session = makeSession();
    session.start();
    latest().open();

    latest().deliver(envelope('narrative_start', { prefix: 'x', mode: 'llm' }));
    expect(session.getSnapshot().narrative.text).toBe('');
    expect(session.getSnapshot().narrative.streaming).toBe(true);

    latest().deliver(envelope('narrative_chunk', { text: 'Attention ', provisional: true }));
    latest().deliver(envelope('narrative_chunk', { text: 'degradation detected.' }));

    expect(session.getSnapshot().narrative.text).toBe('Attention degradation detected.');
    expect(session.getSnapshot().narrative.streaming).toBe(true);
    // Nothing provisional may be kept as history.
    expect(session.getSnapshot().narrativeLog.length).toBe(0);
    session.dispose();
  });

  it('replaces the panel with the final text instead of appending it', () => {
    const session = makeSession();
    session.start();
    latest().open();

    latest().deliver(envelope('narrative_start', { mode: 'llm' }));
    latest().deliver(envelope('narrative_chunk', { text: 'PROVISIONAL DRAFT ONLY' }));
    latest().deliver(
      envelope('narrative_final', { text: 'Final text.', source: 'llm', latency_ms: 420, risk_level: 'MODERATE' }),
    );

    const narrative = session.getSnapshot().narrative;
    expect(narrative.text).toBe('Final text.');
    expect(narrative.text).not.toContain('PROVISIONAL');
    expect(narrative.streaming).toBe(false);
    expect(narrative.source).toBe('llm');
    expect(narrative.latencyMs).toBe(420);
    expect(narrative.hasFinal).toBe(true);

    expect(session.getSnapshot().narrativeLog.length).toBe(1);
    expect(session.getSnapshot().narrativeLog[0]?.text).toBe('Final text.');
    session.dispose();
  });

  it('discards provisional text when the guard rejects a draft', () => {
    const session = makeSession();
    session.start();
    latest().open();

    latest().deliver(envelope('narrative_start', { mode: 'llm' }));
    latest().deliver(envelope('narrative_chunk', { text: 'unsupported claim about heart rate' }));
    latest().deliver(envelope('narrative_retry', { violations: ['rppg_bpm_not_available'] }));

    // The replacement starts over, so the rejected draft must not survive.
    expect(session.getSnapshot().narrative.text).toBe('');
    session.dispose();
  });

  it('keeps the text but stops the spinner when a narrative is cancelled', () => {
    const session = makeSession();
    session.start();
    latest().open();

    latest().deliver(envelope('narrative_start', { mode: 'llm' }));
    latest().deliver(envelope('narrative_chunk', { text: 'partial' }));
    latest().deliver(envelope('narrative_cancelled', {}));

    expect(session.getSnapshot().narrative.text).toBe('partial');
    expect(session.getSnapshot().narrative.streaming).toBe(false);
    session.dispose();
  });

  it('records a template fallback without treating it as an error', () => {
    const session = makeSession();
    session.start();
    latest().open();

    latest().deliver(
      envelope('narrative_final', { text: 'Template text.', source: 'template_fallback', risk_level: 'HIGH' }),
    );

    const narrative = session.getSnapshot().narrative;
    expect(narrative.source).toBe('template_fallback');
    expect(narrative.text).toBe('Template text.');
    // A fallback is a feature, not a transport problem: the socket must stay clean.
    expect(session.getSnapshot().connection).toBe('connected');
    session.dispose();
  });

  it('reports narrative errors as problems while telemetry keeps working', () => {
    const session = makeSession();
    session.start();
    latest().open();

    latest().deliver(envelope('narrative_error', { reason: 'narrative_service_unavailable' }));
    expect(session.getSnapshot().problems.some((p) => p.kind === 'narrative')).toBe(true);

    latest().deliver(envelope('telemetry', { risk_score: 0.3 }));
    expect(session.getSnapshot().telemetryReceived).toBe(1);
    session.dispose();
  });
});

// ---------------------------------------------------------------------------
// history buffer
// ---------------------------------------------------------------------------
describe('history buffer', () => {
  it('accumulates points and stays bounded', () => {
    const session = makeSession({ fps: 100, maxHistory: 5 });
    session.start();
    latest().open();

    for (let i = 0; i < 20; i += 1) {
      latest().deliver(envelope('telemetry', { risk_score: i / 20, p_fatigue: i / 20 }));
    }

    const history = session.getSnapshot().history;
    expect(history.length).toBe(5);
    // The oldest points are evicted, so the newest value is always present.
    expect(history[history.length - 1]?.risk).toBeCloseTo(19 / 20, 5);
    session.dispose();
  });

  it('records a null risk rather than substituting zero', () => {
    const session = makeSession();
    session.start();
    latest().open();

    latest().deliver(envelope('telemetry', { p_fatigue: 0.25 }));
    const point = session.getSnapshot().history[0];
    expect(point?.risk).toBeNull();
    expect(point?.pFatigue).toBe(0.25);
    session.dispose();
  });
});
