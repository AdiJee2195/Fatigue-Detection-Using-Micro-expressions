/**
 * The gateway wire contract, as typed structures.
 *
 * Authoritative source: `CLIENT_CONTRACT.md`. If a field is added or renamed there,
 * it must be renamed here in the same change — there is no code generation, so this
 * file *is* the TypeScript half of the contract.
 *
 * Everything the gateway sends is a tagged envelope. The client dispatches on `type`
 * and never inspects anything else to route a message.
 *
 * Optional fields are optional for a reason: the gateway relays provenance verbatim
 * and never defaults it, so the dashboard must be able to render "unknown" rather
 * than silently assuming a value.
 */

// ---------------------------------------------------------------------------
// envelopes
// ---------------------------------------------------------------------------

export const ENVELOPE_TYPES = [
  'telemetry',
  'telemetry_error',
  'assessment',
  'narrative_start',
  'narrative_chunk',
  'narrative_retry',
  'narrative_final',
  'narrative_cancelled',
  'narrative_error',
] as const;

export type EnvelopeType = (typeof ENVELOPE_TYPES)[number];

export interface Envelope<T = unknown> {
  type: string;
  sessionId: string;
  payload: T;
  /** Gateway clock (epoch millis) when the envelope was built. */
  timestamp: number;
}

/** Vision-service failure reasons the gateway maps errors onto. */
export type TelemetryErrorReason =
  | 'vision_service_timeout'
  | 'vision_service_http_error'
  | 'vision_service_error';

/** Narrative-path failure reasons. */
export type NarrativeErrorReason =
  | 'narrative_service_unavailable'
  | 'malformed_narrative_event'
  | 'missing_type'
  | 'unknown_type'
  | 'invalid_telemetry_frame';

// ---------------------------------------------------------------------------
// telemetry — one row per processed frame
// ---------------------------------------------------------------------------

export interface Telemetry {
  /** Epoch seconds. The gateway rewrites this to be strictly increasing. */
  ts?: number;
  /** P(fatigue) = P(microsleep) + P(yawning). Mirrors risk_score. */
  p_fatigue?: number;
  class_probs?: Record<string, number>;
  /** Eye Aspect Ratio; null when no provider can measure it. */
  ear?: number | null;
  /** FACS intensity 0-5. */
  au?: Record<string, number>;
  face_detected?: boolean;
  /** Mean face-ROI luminance, 0-1. */
  illumination?: number | null;
  /** null until a real rPPG stage exists — the guard rejects ungrounded mentions. */
  rppg_bpm?: number | null;
  risk_score?: number;
  /** The same AU values normalised to 0-1, for gauges. */
  au_intensities?: Record<string, number>;
  active_aus?: Record<string, boolean>;
  inference_latency_ms?: number;
  /** 'stub' | 'onnx'. Null means the vision service did not report it. */
  mode?: string | null;
  /**
   * Canonical provenance, computed by the vision service: were these numbers
   * measured or generated? Distinct from `mode`, which only names the code path.
   * Null means unstated and must never be rendered as LIVE.
   */
  mode_class?: ModeClass | null;
  /** Per-metric provenance, e.g. { ear: 'live', rppg_bpm: 'not_configured' }. */
  signal_status?: Record<string, string> | null;
}

/**
 * Canonical provenance class: the answer to "were these numbers measured?"
 *
 * Deliberately separate from `VisionMode`, which names the pipeline code path.
 * `onnx` mode with an unloaded model is not live; only the vision service can
 * answer this, so the dashboard relays it and never derives it.
 */
export type ModeClass = 'synthetic' | 'live' | 'unknown';

export function asModeClass(value: string | null | undefined): ModeClass {
  if (value === 'synthetic' || value === 'live' || value === 'unknown') return value;
  return 'unknown';
}

/**
 * How a value was obtained. Rendered next to the value it describes; a synthetic
 * or estimated reading must never be presented as a measurement.
 */
export type SignalStatus =
  | 'live'
  | 'synthetic'
  | 'estimated'
  | 'unavailable'
  | 'not_detected'
  | 'not_configured'
  | 'unknown';

/** Human-readable label for a provenance value, for the telemetry table. */
export const SIGNAL_STATUS_LABELS: Record<SignalStatus, string> = {
  live: 'LIVE',
  synthetic: 'SYNTHETIC',
  estimated: 'ESTIMATED',
  unavailable: 'Unavailable',
  not_detected: 'Not detected',
  not_configured: 'Not configured',
  unknown: 'Unknown',
};

/** Provenance values that mean "this is not a real measurement of a person". */
export const NON_LIVE_STATUSES: readonly SignalStatus[] = [
  'synthetic',
  'estimated',
  'unavailable',
  'not_detected',
  'not_configured',
  'unknown',
];

export function asSignalStatus(value: string | undefined | null): SignalStatus {
  switch (value) {
    case 'live':
    case 'synthetic':
    case 'estimated':
    case 'unavailable':
    case 'not_detected':
    case 'not_configured':
      return value;
    default:
      return 'unknown';
  }
}

// ---------------------------------------------------------------------------
// assessment — the deterministic rule engine's verdict, every ~2 s
// ---------------------------------------------------------------------------

export type RiskLevel = 'LOW' | 'MODERATE' | 'HIGH' | 'CRITICAL' | 'UNKNOWN';
export type Trend = 'rising' | 'falling' | 'stable' | 'unknown';
export type Confidence = 'high' | 'medium' | 'low';

export interface Finding {
  id: string;
  /** Operator-facing explanation, produced by the rule engine — never by the UI. */
  plain: string;
  severity: number;
  pre_attentive?: boolean;
  label?: string;
}

export interface Assessment {
  ts_str?: string;
  risk_level?: RiskLevel;
  trend?: Trend;
  p_fatigue_smoothed?: number;
  confidence?: Confidence;
  /** Pre-attentive signs are present while the classifier is still quiet. */
  early_drift?: boolean;
  /** The per-user baseline is still being learned (~first 60 s). */
  calibrating?: boolean;
  reliability?: boolean;
  findings?: Finding[];
  quality_notes?: string[];
  recommended_action?: string;
  signals_available?: string[];

  // ---- provenance ---------------------------------------------------------
  // Copied from the telemetry by the rule engine, so the assessment can state
  // what kind of data produced it. Absent on an older engine.
  mode?: string | null;
  mode_class?: ModeClass | null;
  signal_status?: Record<string, string> | null;

  [key: string]: unknown;
}

/** True when the assessment was computed from generated rather than measured data. */
export function isSyntheticAssessment(assessment: Assessment | null | undefined): boolean {
  return asModeClass(assessment?.mode_class) === 'synthetic';
}

/**
 * The assessment arrives wrapped: `{ type: 'assessment', payload: { assessment: {...} } }`
 * in some gateway builds and unwrapped in others. Accept both rather than
 * guessing wrong and rendering an empty panel.
 */
export function unwrapAssessment(payload: unknown): Assessment | null {
  if (!payload || typeof payload !== 'object') return null;
  const record = payload as Record<string, unknown>;
  const inner = record['assessment'];
  if (inner && typeof inner === 'object') {
    return inner as Assessment;
  }
  return record as Assessment;
}

// ---------------------------------------------------------------------------
// narrative
// ---------------------------------------------------------------------------

export interface NarrativeChunkPayload {
  text?: string;
  provisional?: boolean;
}

/**
 * Why the LLM was, or was not, used. Drives the provider status badge.
 *
 * `unavailable` is a provider failure; `skipped` is a policy decision (a CRITICAL
 * verdict never waits on a network round trip). The two look similar on screen and
 * mean completely different things operationally, so they are not merged.
 */
export type LlmStatus =
  | 'ok'
  | 'unavailable'
  | 'disabled'
  | 'skipped'
  | 'guard_rejected'
  | 'not_called';

/**
 * Coerce a wire value into the union, or `null` when absent.
 *
 * Unknown values become `null` rather than defaulting to `ok`: a status this
 * build does not understand must read as "we do not know", never as success.
 */
export function asLlmStatus(value: unknown): LlmStatus | null {
  switch (value) {
    case 'ok':
    case 'unavailable':
    case 'disabled':
    case 'skipped':
    case 'guard_rejected':
    case 'not_called':
      return value;
    default:
      return null;
  }
}

export const LLM_STATUS_LABELS: Record<LlmStatus, string> = {
  ok: 'OK',
  unavailable: 'Unavailable',
  disabled: 'Not configured',
  skipped: 'Skipped',
  guard_rejected: 'Rejected by guard',
  not_called: 'Pending',
};

/** Classified provider failure, mirroring `fatigue_xai.llm.ErrorKind`. */
export type LlmErrorKind =
  | 'not_configured'
  | 'bad_request'
  | 'unauthorized'
  | 'forbidden'
  | 'rate_limited'
  | 'model_not_found'
  | 'timeout'
  | 'connection'
  | 'server_error'
  | 'malformed_response'
  | 'empty_response';

/**
 * Operator-facing sentence for a classified provider failure.
 *
 * A provider outage must never read as a telemetry outage: the risk assessment is
 * still valid, and the copy has to say so.
 */
export function llmErrorHint(kind: string | null | undefined): string | null {
  switch (kind) {
    case 'not_configured':
      return 'No API key or model is configured; explanations use the deterministic template.';
    case 'unauthorized':
    case 'forbidden':
      // A credential problem is persistent, not transient, so the operator needs
      // both facts: what to fix, and that the risk assessment is still valid.
      return 'The provider rejected the API key. Check the server-side credential; the deterministic explanation is being used.';
    case 'rate_limited':
      return 'The provider is rate limiting; explanations will retry on the next assessment.';
    case 'model_not_found':
      return 'The configured model is not available on this provider account; the deterministic explanation is being used.';
    case 'timeout':
      return 'The provider did not answer in time; the deterministic explanation was used.';
    case 'connection':
      return 'The provider could not be reached; the deterministic explanation was used.';
    case 'server_error':
      return 'The provider returned an error; the deterministic explanation was used.';
    case 'malformed_response':
      return 'The provider returned something unusable; it was discarded.';
    case 'empty_response':
      // Live case: a reasoning model can spend the whole token budget on its
      // reasoning channel and then emit no answer text at all.
      return 'The provider returned no text (a reasoning model may have used the whole token budget); the deterministic explanation was used.';
    default:
      return null;
  }
}

export interface NarrativeStartPayload {
  prefix?: string;
  /** 'llm' | 'template' */
  mode?: string;
  risk_level?: RiskLevel;
  /** Which provider would be asked, e.g. 'groq:llama-3.3-70b-versatile'. Null = none. */
  provider?: string | null;
  llm_status?: LlmStatus;
  bypass_reason?: string | null;
}

export interface NarrativeFinalPayload {
  text?: string;
  /** 'llm' | 'template' | 'template_fallback' — badge the panel when not 'llm'. */
  source?: string;
  provider?: string | null;
  llm_status?: LlmStatus;
  /** Present only when the provider failed; classify it for the operator. */
  error_kind?: LlmErrorKind | string | null;
  guard?: unknown;
  latency_ms?: number;
  first_token_ms?: number | null;
  risk_level?: RiskLevel;
}

export interface NarrativeState {
  /** Accumulated text. Replaced wholesale on `narrative_final`. */
  text: string;
  riskLevel: RiskLevel | null;
  source: string | null;
  streaming: boolean;
  latencyMs: number | null;
  /** True once at least one final narrative has been received this session. */
  hasFinal: boolean;
  /** Which provider produced (or was asked for) this narrative. Null = none configured. */
  provider: string | null;
  /** Why the LLM was or was not used. Null until the first event arrives. */
  llmStatus: LlmStatus | null;
  /** Classified provider failure, when there was one. */
  errorKind: string | null;
}

export const EMPTY_NARRATIVE: NarrativeState = {
  text: '',
  riskLevel: null,
  source: null,
  streaming: false,
  latencyMs: null,
  hasFinal: false,
  provider: null,
  llmStatus: null,
  errorKind: null,
};

/** True when the narrative was not produced by the LLM (guard rejected or disabled). */
export function isFallbackNarrative(source: string | null): boolean {
  return source === 'template' || source === 'template_fallback';
}

// ---------------------------------------------------------------------------
// transport
// ---------------------------------------------------------------------------

/**
 * Connection state machine.
 *
 * ```
 *  idle ──► connecting ──► connected ──► closed        (clean shutdown)
 *              │  ▲            │
 *              │  └────────────┘  (reconnect after a drop)
 *              ▼            ▼
 *          reconnecting   degraded ──► reconnecting    (stale connection)
 *              │
 *              └──► error   (permanent: bad URL / origin rejected / auth)
 * ```
 */
export type ConnectionState =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'degraded'
  | 'error'
  | 'closed';

export const CONNECTION_STATE_LABELS: Record<ConnectionState, string> = {
  idle: 'Idle',
  connecting: 'Connecting',
  connected: 'Connected',
  reconnecting: 'Reconnecting',
  degraded: 'Degraded',
  error: 'Error',
  closed: 'Closed',
};

/** Where the frames come from. Demo mode never claims to be a camera. */
export type FrameSource = 'camera' | 'demo' | 'none';

/** Which pipeline produced the telemetry currently on screen. */
export type VisionMode = 'stub' | 'onnx' | 'unknown';

export function asVisionMode(value: string | null | undefined): VisionMode {
  if (value === 'stub') return 'stub';
  if (value === 'onnx') return 'onnx';
  return 'unknown';
}
