# Fatigue Detect — Java API Gateway

WebSocket gateway for the fatigue-detection pipeline. It ingests webcam frames from a browser
client, forwards each one to the Python vision service, and multiplexes two streams back over a
single connection: fast **telemetry** (per frame) and slower **narrative** events relayed from
the Python XAI engine.

For the full four-process start-up order see [`../README_RUN.md`](../README_RUN.md); the client
message contract is in [`../CLIENT_CONTRACT.md`](../CLIENT_CONTRACT.md).

```
React ──WS──▶ gateway :8080 ──HTTP POST /api/v1/analyze──▶ vision :8000
                   │
                   └──WS (one per session, long-lived)──▶ narrative-xai :8100
                          ◀── assessment / narrative_* events ──┘
```

## Quick start

```bash
cd gateway
mvn spring-boot:run
```

Gateway listens on `http://localhost:8080`; the WebSocket endpoint is
`ws://localhost:8080/ws/video/{sessionId}`.

Start the vision service (`:8000`) and the narrative service (`:8100`) first — the gateway will
start without them, but frames will produce `telemetry_error` envelopes and the bridge will log
reconnect attempts until they appear.

## Configuration

| Property | Default | Description |
|---|---|---|
| `server.port` | `8080` | Gateway HTTP/WebSocket port |
| `vision.service.url` | `http://localhost:8000` | Vision service base URL |
| `vision.service.timeout-seconds` | `2` | Per-request timeout for inference |
| `narrative.service.ws-url` | `ws://localhost:8100` | Narrative XAI WebSocket base URL |
| `narrative.service.reconnect-seconds` | `3` | Delay between narrative reconnect attempts |
| `narrative.service.send-buffer` | `64` | Bounded per-session outbound queue; overflow drops |
| `gateway.backpressure.warn-after-drops` | `50` | Consecutive drops before escalating DEBUG → WARN |
| `gateway.allowed-origins` | `http://localhost:5173,http://localhost:3000` | Allowed browser origins |
| `gateway.max-frame-bytes` | `524288` | Maximum accepted frame payload |
| `gateway.metrics-interval-seconds` | `5` | Interval of the per-session `METRICS` log line |

Every property can be overridden by environment variable, e.g.
`VISION_SERVICE_URL=http://10.0.0.5:8000 NARRATIVE_WS_URL=ws://10.0.0.6:8100 mvn spring-boot:run`.

**There must be exactly one top-level `narrative:` key in `application.yml`.** A duplicate key
makes Spring Boot fail to parse the document at start-up.

### A note on `${...}` placeholders

`vision.service.url` and `narrative.service.ws-url` are declared with environment-variable
defaults, so a plain override works. The `gateway.backpressure.warn-after-drops` value comes
from `@Value` in the handler, not from relaxed binding — set it via the env var shown above.

## Design notes

- **Backpressure: drop, never queue.** If a frame is still in flight to the vision service when
  the next arrives, the new frame is discarded immediately (DEBUG log, `frames_dropped`
  counter). A queued frame would be stale by the time it was processed, and the client is
  already sending fresher data. Consequence: **achieved fps = 1 / vision latency.** Watch
  `effective_fps` in the periodic `METRICS` line.
- **Non-blocking outbound calls.** All downstream I/O uses WebFlux `WebClient` /
  `ReactorNettyWebSocketClient`. No request path calls `.block()`, and there is no `@Scheduled`,
  no broker and no database. The metrics ticker is a `Flux.interval` subscription created per
  session and disposed on close.
- **The gateway does not decide risk.** Narrative *scheduling* lives in the Python engine, which
  owns the sliding windows, the per-user baseline and the level hysteresis. The gateway relays
  every processed frame and passes the engine's events through untouched.
- **Timestamp normalisation.** The browser sends epoch **milliseconds**; the engine needs epoch
  **seconds**, strictly increasing. The gateway converts and clamps (repeated or out-of-order
  client clocks get `previous + 0.001`), because a non-monotonic `ts` makes the engine's blink
  rates and trend slope meaningless.
- **Resilience.** A failing frame produces a `telemetry_error` envelope and the socket stays
  open; the vision service is retried on the next frame. A dead narrative service produces a
  throttled `narrative_error` (at most one per 10 s per session) and the bridge reconnects
  forever; telemetry is unaffected.

## Outbound envelope

Every message the client receives is tagged:

```json
{ "type": "telemetry", "sessionId": "operator-001", "payload": { }, "timestamp": 1699999999123 }
```

`type` ∈ `telemetry`, `telemetry_error`, `assessment`, `narrative_start`, `narrative_chunk`,
`narrative_retry`, `narrative_final`, `narrative_cancelled`, `narrative_error`. Narrative
payloads are relayed verbatim from the engine — see `CLIENT_CONTRACT.md` for how to render them
(`narrative_chunk` is **provisional**; replace the panel on `narrative_final`).

## Tests

```bash
mvn test
```

- `GatewayWebSocketIntegrationTest` drives the real handler over a real socket with the vision
  client and narrative bridge mocked; it asserts one connection is reused, that exactly one
  telemetry envelope appears per processed frame, that a frame arriving mid-flight increments
  `frames_dropped` without producing telemetry, that a vision failure yields
  `telemetry_error` and later recovers, and that malformed frames are ignored.
- `OrchestrationServiceTest` covers the ms → s conversion, strict monotonicity, per-session
  clocks and frame ids, error classification, and that a failing narrative bridge cannot turn a
  good frame into an error.
- `EnvelopeFactoryTest` covers event type mapping and the malformed/unknown fallbacks.
