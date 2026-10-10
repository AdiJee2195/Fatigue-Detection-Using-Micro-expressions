# React client contract

One WebSocket per operator session. Everything the server sends is a **tagged envelope**;
the client dispatches on `type` and never inspects anything else to route a message.

## 1. Connect

```
ws://<gateway-host>:8080/ws/video/<sessionId>
```

- `sessionId` is the operator/device id. **Reuse the same id when you reconnect** — the
  gateway replaces the stale socket for that id and the narrative engine keeps that
  operator's windows and per-user baseline.
- Allowed origins default to `http://localhost:5173,http://localhost:3000`. Anything else is
  rejected at the handshake; configure `GATEWAY_ALLOWED_ORIGINS` on the gateway.
- Reconnect with exponential backoff (cap ~15 s). A reconnect costs the narrative engine's
  in-progress narrative, not the accumulated baseline.

## 2. Send frames

```json
{ "frame": "<base64 JPEG, no data: prefix>", "timestamp": 1699999999123 }
```

- `timestamp` is epoch **milliseconds** at capture. The gateway converts it to seconds and
  guarantees it strictly increases before passing it on.
- Target 15-24 fps, JPEG quality ~0.6, width ≤ 640 px. A full-resolution frame is slower to
  encode than the pipeline is fast, so you lose updates without gaining accuracy.
- **Never queue frames client-side.** Frames are dropped, not queued, end to end. If
  `socket.bufferedAmount > 0`, skip the frame and wait for the next tick — a late frame is
  worse than a missing one.
- Strip the data-URL prefix: `canvas.toDataURL('image/jpeg', q).split(',')[1]`.
- Frames must be ≤ `gateway.max-frame-bytes` (default 512 KB) or the gateway rejects them.

### Frame size: two ceilings, which behave differently

| Ceiling | Default | What happens if exceeded |
|---|---|---|
| Handler limit (`gateway.max-frame-bytes`) | 512 KB | The frame is **rejected and counted nowhere**; the session carries on and the next frame is processed normally. |
| WebSocket container ceiling (limit + 64 KB headroom) | ~576 KB | The message is **never delivered**; the container closes the socket with **1009** and the client must reconnect. |

The headroom exists so that an ordinary client mistake — one oversized frame — costs a
frame rather than the session. A client should never approach either ceiling: at 640 px
width and JPEG quality 0.6 a frame is roughly 40 KB.

## 3. Receive — envelope

```json
{ "type": "telemetry", "sessionId": "operator-001", "payload": { ... }, "timestamp": 1699999999123 }
```

`timestamp` is the gateway's clock (ms) when the envelope was built.

| `type` | `payload` | What to do |
|---|---|---|
| `telemetry` | `{ risk_score, au_intensities, active_aus, inference_latency_ms, ear, class_probs, mode, signal_status }` | Update the gauges. Arrives once per processed frame — the most frequent message. |
| `telemetry_error` | `{ reason }` | Show a degraded-connection indicator. `reason` ∈ `vision_service_timeout`, `vision_service_http_error`, `vision_service_error`. The socket stays open and recovers by itself. |
| `assessment` | the rule engine's `Assessment` | Update level/trend/findings panels. Every ~2 s. |
| `narrative_start` | `{ prefix, mode }` | Begin a new narrative. Reset the panel to `""` and set a "streaming" state. `mode` ∈ `llm`, `template`. |
| `narrative_chunk` | `{ text, provisional: true }` | **Append** to the panel. Text is provisional and unvalidated. |
| `narrative_retry` | `{ violations: [...] }` | The guard rejected a draft; a silent retry follows. Keep appending; optionally clear the panel since the replacement starts over. |
| `narrative_final` | `{ text, source, guard, latency_ms, risk_level }` | **Replace** the whole panel with `text`. Authoritative. Show a small badge when `source != "llm"`. |
| `narrative_cancelled` | `{ ... }` | A more severe alert pre-empted this one. Stop the streaming state; the replacement starts shortly. |
| `narrative_error` | `{ reason, ... }` | Narrative path degraded (`narrative_service_unavailable`, `malformed_narrative_event`, or a rejected frame). Telemetry is unaffected. Throttled to one per 10 s per session. |

### `assessment` payload (the parts a dashboard needs)

```json
{
  "ts_str": "T+00:03:04",
  "risk_level": "HIGH",            // LOW | MODERATE | HIGH | CRITICAL | UNKNOWN
  "trend": "rising",               // rising | falling | stable | unknown
  "p_fatigue_smoothed": 0.72,
  "confidence": "high",            // high | medium | low
  "early_drift": true,             // pre-attentive signs while the model is still quiet
  "findings": [ { "id": "LID_DROOP", "plain": "...", "severity": 2, "pre_attentive": true } ],
  "quality_notes": [],
  "recommended_action": "Advise the operator to take a break at the next safe opportunity.",
  "reliability": true,
  "signals_available": ["model_probabilities", "ear", "facs_au"]
}
```

Treat `UNKNOWN` as "camera/lighting problem", not as "safe": it is what the engine reports
when the face is absent or the data is unusable.

### Provenance: `mode` and `signal_status`

Every `telemetry` payload carries two additive fields. They exist so the dashboard cannot
present generated numbers as measurements:

```json
{ "mode": "stub", "signal_status": { "ear": "synthetic", "rppg_bpm": "not_configured" } }
```

- `mode` is `stub` (synthetic demo pipeline) or `onnx` (real inference). A client should
  show a **DEMO / SYNTHETIC** badge for `stub` and only show **LIVE** for `onnx`.
- `signal_status` gives per-metric provenance: `live`, `synthetic`, `estimated`,
  `unavailable`, `not_detected`, `not_configured`.
- Both may be `null` when an older vision service does not send them. **A client must
  render that as "unknown" and must never assume a missing value is live.** The gateway
  relays them verbatim and never invents a default.

## 4. Narrative panel rules (important)

1. On `narrative_start`, clear the panel and start a streaming indicator.
2. On `narrative_chunk`, append `payload.text` — this text is **provisional and unvalidated**.
3. On `narrative_final`, **replace** the entire panel with `payload.text`. Never append it, and
   never trust the accumulated chunks over it.
4. Badge the panel when `payload.source` is `template` or `template_fallback`: the LLM output
   was rejected by the factual guard and a deterministic template was used instead. This is a
   feature (accuracy never depends on the model) and supervisors should be able to see it.
5. On `narrative_cancelled`, keep the text but stop the streaming indicator — a higher-severity
   narrative is about to replace it.

## 5. Reading the risk model

- `risk_score` / `p_fatigue_smoothed` is a probability, not a severity. Always pair it with
  `risk_level`, which additionally accounts for findings, corroboration and hysteresis.
- `p_fatigue_smoothed` uses a ~5 s EMA; `trend` needs ~30 s of history before it stops being
  `unknown`. A freshly connected session reports `calibrating: true` for the first ~60 s while
  the per-user eye-openness baseline is learned — the pre-attentive rules need that baseline,
  so do not present findings as final during calibration.
- `early_drift: true` is the headline case this project exists for: pre-attentive signs
  (lid droop, fixed stare, brow/lid strain) are present while the classifier is still quiet.
  Surface it even when `risk_level` is only `MODERATE`.

### Reference implementation

The production client is in this repository, not a sketch:

- `frontend/src/types.ts` — the typed contract (envelopes, telemetry, assessment, narrative)
- `frontend/src/lib/FatigueSession.ts` — connection state machine, drop-don't-queue frame
  loop, bounded history and narrative buffers
- `frontend/src/lib/useFatigueSession.ts` — the React adapter
- `frontend/tests/FatigueSession.test.ts` — the state machine, narrative rules and
  backpressure behaviour, tested with an injected fake socket
- `frontend/src/components/` — the panels (risk, telemetry, timeline, explainability,
  narrative, diagnostics)

`frontend/src/lib/backoff.ts` implements the reconnect policy: exponential with full
jitter, floored at 750 ms and capped at 15 s, so a fleet of clients cannot reconnect in
lockstep.

### Close codes the gateway sends

The reason string is part of the contract — code `1008` is a generic policy violation, so
the client must read the reason to know which case it is.

| Code | Reason | Meaning and required client behaviour |
|---|---|---|
| `1003` | – (no reason) | The URL did not carry a usable session id. Permanent: do not retry, surface it. |
| `1008` | `session_already_active` | Another **live** connection still owns this id. Transient: the gateway releases it after `gateway.session-takeover-grace-ms` of silence, so reconnect with backoff. This is what a React StrictMode remount or a fast reload hits, and it must not park the operator on an error card. |
| `1013` | `gateway session capacity reached` | The gateway is at `gateway.max-sessions`. Retryable with backoff, but say so in the UI: it is an operator-visible condition, not a network fault. |
| `1009` | – | A message exceeded the container ceiling. The client must reconnect; see the two-ceiling table above. |

An origin mismatch does not produce any of these: Spring rejects the **handshake** with HTTP
403, which a browser reports as an abnormal `1006` closure with no reason. Do not map
`1008` onto "origin rejected" — editing `GATEWAY_ALLOWED_ORIGINS` will not help.
