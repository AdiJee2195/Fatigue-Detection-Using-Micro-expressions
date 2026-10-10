# Project Analysis — Pre-Attentive Cognitive Fatigue Detection Platform

**Date:** 2026-10-09
**Method:** static inspection of every source file + execution of all four test suites + live reproduction of each bug claimed below.
**Rule applied:** nothing in this document is reported as verified unless a command was run and its exit status inspected. Section 6 separates the two explicitly.

---

## 1. What the system is

A four-process pipeline that watches a driver's face and answers one question — *"is this person's visual attention starting to degrade, before they visibly fall asleep?"* — with a **deterministic risk verdict plus a written explanation of why**.

The architecture is deliberately minimalist: **no database, no message broker, no Reactor-based server, no scheduler.** All state is in-memory and per-session. That is a *design decision*, not an omission, and it is defensible in a viva: the system is a stateless-per-frame streaming pipeline, so durability would buy nothing and cost operational surface area. The narrative engine keeps a ~60 s per-operator baseline in memory and rebuilds it after a restart — the correct trade against a database for this workload.

**Honest one-line description:** a real-time streaming pipeline with a genuine deterministic rule engine and a genuinely guarded explanation layer, fed — as shipped — by **synthetic** vision telemetry.

---

## 2. Tech stack (exact, verified from lockfiles/manifests)

### Gateway — Java
| Item | Value | Source |
|---|---|---|
| Java | release **17** (project); built here on JDK **22.0.2** | `gateway/pom.xml:21` |
| Spring Boot | **3.4.1** (parent) | `gateway/pom.xml:10` |
| Starters | `spring-boot-starter-web`, `-websocket`, `-webflux`, `-test` | `gateway/pom.xml` |
| Lombok | managed by Boot (annotation processing verified) | `gateway/pom.xml:46` |
| Build tool | **Maven** (no wrapper committed) | — |
| Embedded WS container | Tomcat **10.1.34** | test log |

`spring-boot-starter-webflux` is present only for `WebClient` + `Mono`/`Flux` as an HTTP/async client and for `Flux.interval` tickers. **No reactive server is started** (Tomcat, not Netty, serves traffic — confirmed in the test log). This is the right call: it keeps `OrchestrationService` non-blocking without introducing WebFlux's server.

### Vision service — Python
| Item | Value |
|---|---|
| Runtime | Python **3.12.0** (`.venv`) |
| Framework | FastAPI, Uvicorn ≥0.27, Pydantic ≥2.5 |
| Numerics | numpy ≥1.26 |
| Optional (ONNX mode only) | pillow ≥10, **onnxruntime ≥1.17**, opencv-python-headless ≥4.9 |
| Optional face backend | mediapipe (commented out — not installed) |

### XAI narrative engine — Python
| Item | Value |
|---|---|
| Framework | FastAPI, Uvicorn, Pydantic |
| HTTP client | httpx ≥0.26 |
| LLM integration | Ollama (`/api/...`) or any OpenAI-compatible `/v1/chat/completions` |
| Agent surface | **MCP** (`mcp>=1.2`) |
| Dev deps | websockets ≥12, pytest ≥8 |

### Frontend — TypeScript
| Item | Version |
|---|---|
| React / React DOM | **18.3.1** |
| Build | **Vite 5.4.11** |
| Language | **TypeScript 5.6.3** (strict, `tsc --noEmit` in the build) |
| Styling | **Tailwind CSS 3.4.17** + PostCSS 8.4.49 + autoprefixer 10.4.20 |
| Charts | **Recharts 3.1.2** |
| Tests | **Vitest 2.1.8** |
| Extra deps | **none** — no router, no Redux, no UI kit. `@vitejs/plugin-react` 4.3.4 only |

### Orchestration
Docker Compose, 5 services (`frontend`, `vision`, `narrative-xai`, `gateway`, and a profile-gated `ollama`). One named volume (`ollama`). **No data volume, no database service** — consistent with the design principle.

### Deliberately absent (and this is correct)
Postgres/Redis/Kafka/RabbitMQ, Spring WebFlux *server*, Spring Security, Kubernetes, Prometheus/Grafana, a JS state manager, a component library.

---

## 3. Workflow charts

### 3.1 Runtime topology and the four hops

```
┌──────────────────┐        ┌──────────────────────┐        ┌─────────────────────┐
│  Browser         │        │  Gateway             │        │  Vision service     │
│  React + Vite    │        │  Spring Boot 3.4.1   │        │  FastAPI            │
│  :5173           │        │  Tomcat :8080        │        │  :8000              │
└────────┬─────────┘        └──────────┬───────────┘        └──────────┬──────────┘
         │                             │                               │
         │ (1) WS  /ws/video/{sid}     │                               │
         │     {"frame":"<b64>",       │                               │
         │      "timestamp":1699…}     │                               │
         ├────────────────────────────►│                               │
         │                             │ (2) POST /api/v1/analyze      │
         │                             │     {frame,timestamp,sessionId}│
         │                             ├──────────────────────────────►│
         │                             │                               │ decode
         │                             │                               │ → face crop
         │                             │                               │ → classifier
         │                             │  (3) 200 InferenceResult      │
         │                             │◄──────────────────────────────┤
         │  (4) envelope {"type":      │                               │
         │      "telemetry", …}        │                               │
         │◄────────────────────────────┤                               │
         │                             │                               │
         │                             │ (5) WS  ws://…:8100/ws/{sid}  │  ┌────────────────────┐
         │                             │     TelemetryFrame (per frame)│  │  XAI engine        │
         │                             ├───────────────────────────────┼─►│  FastAPI :8100     │
         │                             │                               │  │  ├ rules.py        │
         │                             │  (6) assessment / narrative_* │  │  ├ guard.py        │
         │                             │◄──────────────────────────────┼──┤  └ narrative.py    │
         │  (7) envelope relayed       │                               │  └─────────┬──────────┘
         │      verbatim               │                               │            │ optional
         │◄────────────────────────────┤                               │            ▼
         │                             │                               │     Ollama / OpenAI-compat
         │  (8) GET /health /sessions  │                               │     (deterministic template
         │◄────────────────────────────┤                               │      if unavailable)
```

**Note on (1):** the browser must reach the gateway **directly**. `nginx` deliberately does *not* proxy the WebSocket (`frontend/nginx.conf`) — a proxy would introduce a queue in front of a pipeline whose entire design is drop-don't-queue. This is a genuinely good, unusual decision worth defending in a viva.

**Note on (8):** `/health` and `/sessions` exist because the browser **cannot observe backpressure**. Frames are dropped silently by design, so without these the diagnostics panel would have to guess — and a guessed metric is worse than no metric.

### 3.2 Frame lifecycle inside the gateway (the backpressure gate)

```
handleTextMessage(payload)
   │
   ├─ null/empty?                        → drop, log
   ├─ payload.length > maxFrameBytes?    → drop, log  (session survives)
   ├─ JSON parse fails?                  → drop, log
   │
   ├─ AtomicBoolean.compareAndSet(false,true)     ◄── THE GATE
   │      │
   │      ├─ false (a vision call is still in flight)
   │      │     → metrics.recordDropped(); return   ◄── drop, never queue
   │      │
   │      └─ true → clear drop counter, hand off to OrchestrationService
   │                 NON-BLOCKING; the Mono callback clears the flag when done
   ▼
OrchestrationService.processFrame()
   ├─ monotonic frame id + timestamp normalisation (strictly increasing ts)
   ├─ VisionServiceClient POST (async, timeout)
   │     ├─ success → envelope "telemetry"                     → browser
   │     └─ failure → envelope "telemetry_error" (+reason)     → browser
   └─ NarrativeBridge.send(TelemetryFrame)                     → XAI WS
         └─ (fire-and-forget; a narrative failure never blocks or fails telemetry)

afterConnectionClosed()
   └─ dispose in-flight vision call, stop metrics ticker,
      close narrative WS, clear session state, remove registry entries
```

**Why drop-don't-queue:** a queued frame is stale by the time it is processed, and the client will send a fresher one within ~66 ms. Queueing converts a latency problem into a *correctness* problem (the driver's state shown is from 2 seconds ago). Dropping is the right trade, and it is documented rather than accidental.

### 3.3 Narrative engine internals

```
frame → FacsRuleEngine.ingest()
          ├─ rolling window per session (~60 s baseline warm-up)
          ├─ FACS rule evaluation → List[Finding]  (id, severity, plain, pre_attentive)
          ├─ level: LOW | MODERATE | HIGH | CRITICAL   (+ raw_level)
          ├─ trend: rising | falling | stable  (slope/min)
          ├─ confidence: high | medium | low
          └─ early_drift, calibrating, reliable, quality_notes
        emitted ~every 2 s as  {"type":"assessment"}

assessment → NarrativeService.narrate()
          ├─ start  : {"type":"narrative_start", mode, ts_str, level}
          ├─ stream : {"type":"narrative_chunk"}      ← PROVISIONAL, unvalidated
          ├─ guard.py validates the draft against the FACTS view (no 2nd LLM):
          │     rejects invented numbers, wrong direction vs trend,
          │     ungrounded "bpm/heart/pulse", medical/advice language, markdown
          │      ├─ reject → {"type":"narrative_retry"}, regenerate (silent)
          │      └─ accept → {"type":"narrative_final"}   ← AUTHORITATIVE
          └─ a more severe alert may pre-empt → {"type":"narrative_cancelled"}

UI rule: narrative_chunk is PROVISIONAL. narrative_final.text replaces it.
```

**Why this design is strong:** the LLM is *never* authoritative. The facts (level, findings, evidence) come from the rule engine; the guard is rule-based, not an LLM; and any rejection falls back to a deterministic template. This satisfies "do not let LLM output override deterministic safety facts" at the architecture level, not by convention.

### 3.4 Client connection state machine

```
IDLE ──connect()──► CONNECTING ──onopen──► CONNECTED
                        │                       │
                        │ onerror/onclose       │ no telemetry while sending
                        ▼                       ▼  (staleAfterMs = 8 s)
                    RECONNECTING ◄─────────── DEGRADED
                        │  exponential backoff + countdown
                        │
                        └── attempt fails N times / fatal ──► ERROR
                                                stop() ──► CLOSED
```
Implemented in `frontend/src/lib/FatigueSession.ts` with a bounded history buffer (`maxHistory: 900` points, `maxNarrativeLog: 25`, `maxProblems: 20`) — so a multi-hour session cannot grow memory without limit. Bounded client history was an explicit requirement and it is met.

### 3.5 Startup order and dependency handling

```
docker compose up --build
  vision        (:8000)  healthcheck → python urllib /health
  narrative-xai (:8100)  healthcheck → python urllib /health
  gateway       (:8080)  depends_on: service_started (NOT service_healthy)
                         healthcheck → curl /health, start_period 25 s
  frontend      (:5173 → nginx :8080)  depends_on: service_started
  ollama        (:11434) profiles: [llm]  ← opt-in only
```
`depends_on: service_started` (not `service_healthy`) is intentional and tolerable: the gateway is *designed* to run with both downstreams down, so it reports `telemetry_error` and keeps reconnecting the narrative socket. The system degrades instead of failing to boot. Verified in passing — the gateway's own test suite exercises exactly this (a `connection refused` path is asserted).

---

## 4. APIs

### 4.1 What already exists (all first-party — no external API needed to run)

| # | Surface | Method | Path | Producer | Consumer |
|---|---|---|---|---|---|
| 1 | **Frame ingest** | WS | `ws://…:8080/ws/video/{sessionId}` | browser | gateway |
| 2 | Gateway health | GET | `http://…:8080/health` | gateway | browser, orchestrator |
| 3 | Session metrics | GET | `http://…:8080/sessions` | gateway | browser |
| 4 | **Inference** | POST | `http://…:8000/api/v1/analyze` | gateway | vision |
| 5 | Vision health | GET | `http://…:8000/health` | vision | orchestrator, gateway ops |
| 6 | **Narrative stream** | WS | `ws://…:8100/ws/{session_id}` | gateway | XAI |
| 7 | XAI health | GET | `http://…:8100/health` | XAI | orchestrator |
| 8 | Stateless preview | POST | `http://…:8100/v1/narrative/preview` | Postman / eval harness | XAI |
| 9 | Session assessment | GET | `http://…:8100/v1/sessions/{id}/assessment` | ops | XAI |
| 10 | Session narratives | GET | `http://…:8100/v1/sessions/{id}/narratives?limit=20` | ops | XAI |

Plus an **MCP server** in `xai_narrative/` exposing the narrative tools to an agent host.

**Outbound envelope contract** (gateway → browser), discriminated on `type`:
`telemetry`, `telemetry_error`, `assessment`, `narrative_start`, `narrative_chunk`, `narrative_retry`, `narrative_final`, `narrative_cancelled`, `narrative_error`.

### 4.2 What you need to add — and what you do **not** need

**No third-party API is required for the project to run.** Demo mode is fully self-contained. That is worth stating plainly in a viva, because the common failure mode for this kind of project is to bolt on cloud services it does not need.

The three things that would extend it beyond demo are **not APIs** in most cases:

| Need | What it actually is | Why you need it | Cost/effort |
|---|---|---|---|
| **Real fatigue inference** | a trained `.onnx` model **file** + `.onnx.data` | not shipped and deliberately gitignored; `VISION_MODE=onnx` therefore cannot run until you mount one. No inference claim can be made until it does. | requires training or obtaining a model; not an API call |
| **LLM-authored narratives** *(optional)* | HTTP API: Ollama (`/api/chat`) **or** any OpenAI-compatible `/v1/chat/completions` (vLLM, llama.cpp, OpenAI) | only improves *fluency*. `NARRATIVE_LLM=none` already produces fully functional deterministic narratives. | env vars only |
| **Better face detection** *(optional)* | MediaPipe or OpenCV — **libraries**, not APIs | improves face-box quality vs. the Haar/centre-crop fallback | pip install |
| **Identity / multi-tenancy** | an auth provider or a session-token scheme | see Issue #4 — currently any caller who knows a session id can read another operator's data | a design change, not a dependency |

**If you want to *deploy* this beyond your laptop, the missing APIs are all inbound, not outbound:**

1. **Authentication on the WebSocket handshake.** Today the only check is `Origin` (browser-enforced only; trivially forged by any non-browser client).
2. **Authorization on the XAI REST endpoints 9 and 10.** They return *any* session's assessment and narrative history to *any* unauthenticated caller. Guessing `operator-001` is enough. This is an IDOR.
3. **CORS on the XAI REST endpoints.** They have none, so a browser cannot call them at all — which is why they are currently only usable from Postman/curl. Fine today, a blocker if the dashboard ever needs narrative history.
4. **A reverse proxy with TLS** (`wss://`) if it leaves localhost.
5. **An explicit API-key or rate-limit gate** on `/api/v1/analyze` and the frame WebSocket, since a single session can push 512 KB messages as fast as the network allows and there is **no session cap** (see Issue #5).

**Not needed, and I recommend against adding:** a database, Kafka, Redis, Grafana, Elasticsearch, a cloud vision API, a second LLM as a "guard". Each one adds a failure mode to a system whose main virtue is that a frame can be lost without anything else degrading.

---

## 5. Issues

Severity: **P0** = blocks a claimed capability or is a live correctness/security defect; **P1** = real defect, workaround exists; **P2** = drift / polish.

### P0

#### 1. The real-inference path has never executed — no model is shipped, by design
`find . -name "*.onnx*"` returns **nothing**, and `onnxruntime` / `mediapipe` / `opencv-python-headless` are absent from the working venv.

**To the project's credit, this is deliberate and documented, not an oversight.** `.gitignore` excludes `*.onnx`, `*.onnx.data`, `models/` and `xai_narrative/models/` under the comment *"Model weights are large and are not part of the repository; mount them at run time."* `README.md:130` says to *mount* the model directory; `docker-compose.yml` says *"switch to onnx after mounting a model"*; `README.md:148-154` documents `VISION_MODE`, `VISION_FALLBACK_TO_STUB` and `FATIGUE_ONNX` accurately. The design also handles absence correctly: `VISION_FALLBACK_TO_STUB=false` makes a misconfigured deployment report `DEGRADED` and **refuse** to serve synthetic data rather than silently faking it. That is exactly right and should be shown off in a viva.

**So this is not a broken claim — it is a verification gap, and it is the largest one in the project.** Everything downstream of the classifier (EAR/AU extraction, `signal_status` provenance, the UI's LIVE VISION badge, the ONNX branch of `pipeline.py`) has **never been executed on this machine**, because the artifact that would exercise it does not exist here. `README.md:191` is candid that one XAI test is skipped without `FATIGUE_ONNX` — the same honesty should be applied to the vision service.

**Fix:** add `models/README.md` stating plainly that no model ships, how to obtain or train one, and that the inference path is unverified as shipped. That single file converts an unanswerable viva question ("does your model work?") into a defensible answer ("the pipeline is built and tested against the documented contract; the weights are yours to mount, and here is the exact interface they must satisfy").

#### 2. XAI narrative service: `PORT=0` silently moves it to a random port
`xai_narrative/fatigue_xai/api.py`, last line (line 150):
```python
uvicorn.run("fatigue_xai.api:app", host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8100")))
```
**Reproduced live:** `PORT=0 python -m fatigue_xai.api` → `Uvicorn running on http://0.0.0.0:52768`. Uvicorn reads `0` as "bind any free port".

**Why it matters:** the gateway connects to a *fixed* address (`NARRATIVE_WS_URL=ws://localhost:8100`). A random port is indistinguishable from the service being down, so **the entire narrative path dies with a confusing symptom** and nothing in the logs says why. `PORT=0` is exported by some CI harnesses and container platforms.

**This is worse than it looks: the vision service already has the fix.** `vision_service/app.py` (`resolve_port()`) validates the range and has a docstring warning about *this exact failure mode* — including naming `PORT=0`. The fix was written once and never applied to the XAI service. That is the clearest evidence in the repo that the two Python services drifted.

**Fix:** make `resolve_port()` shared (it is a 12-line function) and use it in both.

#### 3. Same file: `PORT=abc` crashes with a raw `ValueError` traceback
**Reproduced live:**
```
File ".../fatigue_xai/api.py", line 150, in <module>
    uvicorn.run(..., port=int(os.getenv("PORT", "8100")))
ValueError: invalid literal for int() with base 10: 'abc'
```
Exit code 1, no friendly message. `resolve_port()` skips unparsable values and falls back to the default. Same fix as #2.

#### 4. No authentication, and session-id reuse is a *takeover*, not just an eavesdrop
`SessionRegistry.register()` (`gateway/…/websocket/SessionRegistry.java`):
```java
WebSocketSession previous = sessions.put(sessionId, session);
if (previous != null && previous.isOpen()) { previous.close(); }
```
Anyone who can guess or observe a `sessionId` (`operator-001` is the documented default!) does **two** things:
- **reads** that operator's full telemetry and narrative stream, and
- **evicts** the legitimate operator's socket.

`WebSocketConfig` sets `setAllowedOrigins(...)`, but `Origin` is a header the *browser* enforces — `curl`/`websocat` set it to anything. So the only real gate is "know the id".

**Honest framing for a report:** acceptable for a localhost research prototype with one operator; **not** acceptable for anything deployed. The honest mitigation today is to treat `sessionId` as a secret capability token (server-issued UUID, never the documented default) rather than an identifier.

#### 5. No cap on concurrent sessions, and each one reserves ~576 KiB
`maxTextMessageBufferSize = maxFrameBytes + 128 + 64 KiB` = `524288 + 128 + 65536` = **589 952 bytes ≈ 576 KiB per session**, allocated by Tomcat. `SessionRegistry.sessions`, `inFlight`, `consecutiveDrops` and `metricsMap` are all unbounded maps.

Tomcat's default `maxConnections` is 8192 → a theoretical **~4.5 GiB** (`8192 × 589 952 B`) of buffers. There is also no per-session frame rate limit, so a hostile client can drive receive-and-decode at line rate. Combined with #4 (no auth), this is a trivial memory/CPU exhaustion path.

**Fix:** a configurable `gateway.max-sessions` (reject 1013/`SERVER_OVERLOADED` past it) and a simple token-bucket on inbound frames.

### P1

#### 6. Narrative error reasons do not match the frontend's typed contract
| Producer | Reason string actually sent |
|---|---|
| `EnvelopeType.REASON_MISSING_TYPE` | `narrative_event_missing_type` |
| `EnvelopeType.REASON_UNKNOWN_TYPE` | `unknown_narrative_event_type` |
| Python `api.py` (invalid frame) → `EnvelopeFactory` copies `message` | `"invalid telemetry frame"` (**spaces**) |
| `frontend/src/types.ts` `NarrativeErrorReason` declares | `missing_type`, `unknown_type`, `invalid_telemetry_frame` |

All three live reasons fall through `reasonMessage()` in `FatigueSession.ts` to the `default` branch, so the operator sees `Reported problem: invalid telemetry frame` instead of the intended *"The explanation engine rejected a telemetry frame."*

**No crash** — the `default` branch saves it. But the declared union type `NarrativeErrorReason` **never matches a real value**, which means it is dead code that gives false confidence, and the gateway can rename a reason string without the compiler noticing. This is precisely the class of bug the typed contract exists to prevent.

**Fix:** one source of truth for reason strings, and make the Python side emit the canonical token (`invalid_telemetry_frame`, no spaces) rather than prose.

#### 7. Provenance (`mode`, `signal_status`) is dropped before it reaches the narrative engine
`InferenceResult` carries both `mode` and `signal_status` (per-metric provenance: `live` / `synthetic` / `not_configured` / …). The frontend consumes them and labels the UI correctly (**it does** — `App.tsx` has a sticky `VisionMode`, a LIVE VISION badge, and a warning when `onnx` mode is fed demo frames).

But `TelemetryFrame.from(result, frameId)` does **not** copy either field, and the Python `FrameTelemetry` schema has **no** `mode` or `signal_status` field at all. A grep for `synthetic|stub|demo` across `xai_narrative/fatigue_xai/*.py` finds hits only in `simulator.py` — **the rule engine, the guard and the narrator are entirely blind to whether the data is real.**

**Consequence:** with `NARRATIVE_LLM=openai`, the generated prose describes synthetic stub telemetry in exactly the same language it would use for a measurement of a real driver. The dashboard says `SYNTHETIC`; the narrative text does not. For a research project whose explicit rule is *"do not claim real-time computer vision when running stub mode"*, that is a real gap — and it is the first question a sharp examiner will ask.

**Fix:** add `mode`/`signal_status` to `FrameTelemetry`, forward them in `TelemetryFrame.from`, and have `narrative.py` prefix or annotate the narrative when `mode == "stub"`. Small change, closes a large honesty hole.

#### 8. XAI slow-consumer drop can discard a `narrative_final`
```python
except asyncio.QueueFull:   # slow consumer: drop oldest non-final events
    try:
        queue.get_nowait()          # ← drops whatever is oldest, including a final
    except asyncio.QueueEmpty:
        pass
    queue.put_nowait(ev)
```
The comment promises "non-final"; the code drops the oldest event unconditionally. `narrative_final` is the **only** authoritative text — the UI is documented to replace provisional chunks with it. If it is dropped, the dashboard is left displaying provisional text permanently, with no error.

The window is narrow (`maxsize=256`), but the comment and the behaviour disagree, and the failure is silent. **Fix:** scan for and evict the oldest *non-final* event, or make the queue unbounded for `*_final`. Also: `put_nowait` inside the handler can itself raise again and propagate into the receive loop — worth a guard.

#### 9. One stale test count in `README.md:188`
`README.md:188` says `# Vision service — 32 tests`. The suite actually runs **42**. Every other count in the project is accurate: `README.md:185` (36 gateway), `:191` (63 + 1 skipped), `:194` (42 frontend), `README_RUN.md:136-139`, and `ENGINEERING_REPORT.md:140-145` all match what I measured. The "18/18 gateway tests" figure that circulates in the handover notes is stale but **appears in no project document** — it is worth correcting wherever it is quoted, not in the repo.

Minor in isolation; worth fixing precisely because the rest of the project is this careful about numbers.

### P2

#### 10. `NARRATIVE_LLM` default contradicts itself across four files
| Location | Value |
|---|---|
| `.env.example:77` | `none` |
| `xai_narrative/.env.example:2` | `ollama` |
| `xai_narrative/Dockerfile:7` | `ENV PORT=8100 NARRATIVE_LLM=ollama …` |
| `docker-compose.yml:45` | `none` |

The root `.env.example` and Compose agree (`none`); the service's own `.env.example` and Dockerfile say `ollama`. So `docker compose up` and `docker build` of the XAI image disagree about the default narrative backend. Since `ollama` is behind `profiles: [llm]`, the Dockerfile default means **the image expects an Ollama host that is not running by default** — it degrades to the template (correct behaviour), but the logs will emit a connection warning on every boot for no reason. Pick `none` and make all four agree.

#### 11. No Maven wrapper, and Maven is not on `PATH`
No `gateway/mvnw` / `mvnw.cmd` / `.mvn/` — verified. `mvn` is not installed on this machine, so **a second developer cannot build the gateway from the repo as it stands**; they must install Maven first. `README_RUN.md` describes the Maven steps but the project does not carry the tool. This directly contradicts the stated goal *"run locally by another developer"*.

**Fix:** `mvn -N wrapper:wrapper` and commit `mvnw`, `mvnw.cmd`, `.mvn/wrapper/`. This also pins the Maven version, which removes the offline-reactor fragility noted in §6.

#### 12. Docker is not installed here, so Compose has never actually been run
`docker: NOT FOUND`. `docker-compose.yml` **parses** (verified in a prior pass) and is unusually well-commented, but *no container in this project has ever been built or started on this machine*. Any statement that the stack "runs with `docker compose up`" is currently a **prediction**, not a result. Either install Docker and prove it, or keep saying "designed to run under Compose; not yet executed."

#### 13. No CI workflow
`.github/` exists but contains **no workflows** (`ls -la .github` shows only `.` and `..`). Four suites, 183 passing tests, and nothing runs them automatically. A CI job is the cheapest possible credibility upgrade for this project.

#### 14. Not a git repository
`git rev-parse` → `fatal: not a git repository`. No history, no blame, no reviewable diff, no `git bisect`. For a capstone that will be assessed and iterated on, this is the highest-value/lowest-effort fix on the list.

**Note in the project's favour:** `.gitignore` is already thorough and correct — secrets, `target/`, `.venv/`, `node_modules/`, `frontend/dist/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.DS_Store`, and the model artefacts. Initialising git costs minutes and immediately yields a correct repository. The 14 stray `.DS_Store` files on disk are already ignored and are cosmetic only.

#### 15. Stale `xai_narrative/docker-compose.snippet.yml`
A 590-byte compose fragment lives inside the XAI service directory alongside the real root `docker-compose.yml`. Two sources of truth for the same deployment; the snippet is not referenced by the root file and will rot. Delete it or fold it into the root.

#### 16. No lint script anywhere, though `.gitignore` expects lint tooling
The frontend's scripts are `dev`, `build`, `typecheck`, `test`, `preview` — no `lint`. `tsc` covers types but nothing catches unused code, floating promises, or hook-dependency mistakes. Meanwhile `.gitignore` already reserves `.mypy_cache/` and `.ruff_cache/`, i.e. linting was intended for the Python side and never added: neither tool is configured, installed, or invoked.

(Listed as P2 on purpose: adding ESLint or Ruff means adding transitive packages, which cuts against the "no unnecessary dependencies" principle. Either add one tool to both stacks, or delete the cache entries so the `.gitignore` stops promising tooling that does not exist. What you should not do is leave it ambiguous.)

#### 17. XAI REST endpoints have no CORS, and that is undeclared
`/v1/narrative/preview`, `/v1/sessions/{id}/assessment`, `/v1/sessions/{id}/narratives` are unreachable from a browser. That is *fine* today (they are Postman/eval surfaces) but it is nowhere stated as an intentional limitation, and it silently blocks the obvious future feature (a "narrative history" view) — which would then be implemented by proxying through the gateway, arguably the better answer anyway. Worth one sentence in the docs. (See also Issue #4: these endpoints return any session's data to any unauthenticated caller.)

---

## 6. Verified vs. not verified

**Everything in this table was executed in this session.** Where a suite was run through a pipe, the exit status of the tested command was captured (`PIPESTATUS[0]`), not the pipe's.

| Check | Exact command | Result | Status |
|---|---|---|---|
| Gateway tests | `mvn -B test` (Maven 3.9.9, JDK 22) | **36 run / 0 failures / 0 errors**, BUILD SUCCESS, 8.9 s | ✅ PASS |
| Vision tests | `.venv/bin/python -m pytest vision_service/tests -q` | **42 passed**, 0.55 s, exit 0 | ✅ PASS |
| XAI tests | `.venv/bin/python -m pytest xai_narrative/tests -q` | **63 passed, 1 skipped**, 7.92 s, exit 0 | ✅ PASS |
| Frontend typecheck | `npm run typecheck` | clean, exit 0 | ✅ PASS |
| Frontend tests | `npm test` | **42 passed** (2 files), exit 0 | ✅ PASS |
| Frontend build | `npm run build` | built in 6.87 s; `index-*.js` 54.87 kB, `charts-*.js` 450.68 kB, exit 0 | ✅ PASS |
| Bug #2 reproduction | `PORT=abc python -m fatigue_xai.api` | `ValueError` traceback, exit 1 | ✅ REPRODUCED |
| Bug #3 reproduction | `PORT=0 python -m fatigue_xai.api` + `lsof` | bound **52768**, not 8100 | ✅ REPRODUCED |
| Model file present | `find . -name "*.onnx*"` | **none** — deliberate: `.gitignore` excludes weights, to be mounted at run time | ❌ ABSENT (BY DESIGN) |
| Docker available | `command -v docker` | **NOT FOUND** | ⛔ BLOCKED |
| Maven on PATH | `command -v mvn` | **NOT ON PATH** (downloaded to `/tmp/mvn` to run the suite) | ⚠️ ENV GAP |
| Maven wrapper committed | `ls gateway/mvnw` | **No such file** | ❌ ABSENT |
| CI workflows | `ls -la .github` | no workflows | ❌ ABSENT |
| Git repo | `git rev-parse --is-inside-work-tree` | **not a git repository** | ❌ ABSENT |

**Note on the build warnings observed:** `mvnd`/surefire emitted no failures, and the JDK-22-on-a-Java-17-target build is clean. The offline-only surefire realm problem seen earlier is an artefact of a cold `~/.m2` and is **not** a project defect.

### Explicitly NOT verified — do not claim any of these

| Claim | Reality |
|---|---|
| Four-process end-to-end run | **NOT EXECUTED.** Docker is absent; no browser session has been driven against a live gateway + vision + XAI stack. The individual components are tested; the *composition* is not. |
| Real ONNX inference | **NOT RUNNABLE HERE.** No model file exists on this machine (it is gitignored by design and must be mounted). The inference path has never executed. |
| Real EAR / AU / rPPG extraction | **NOT IMPLEMENTED** for real. Stub values are synthetic; `rppg_bpm` is `null`/`not_configured` by design. |
| ONNX model = the research architecture | **Already disclosed as FALSE by the project itself.** `README.md:254` states *"The deployed model is not the research architecture"* and names the exported classifier. Keep that sentence — it is exactly the disclosure the rule requires, and it makes this row a *credit*, not a defect. The residual risk is only that a reader stops at the architecture section and never reaches the limitations section. |
| Docker Compose actually boots | **NOT VERIFIED.** Config parses; nothing has ever been built or run. |
| Clinical / real-world fatigue accuracy | **NOT AVAILABLE.** No accuracy figure of any kind exists and none should be quoted. |
| Python suites in the *Docker* images | **NOT VERIFIED.** Only the host `.venv` (Python 3.12) was run. |
| Browser-visible behaviour | **NOT VERIFIED.** The frontend builds and its unit tests pass; no human or automated browser session has been exercised. |

---

## 7. Recommended fix order

**Do first (hours, high credibility payoff)**
1. `git init` — `.gitignore` is already correct and needs no edits. *(#14)*
2. `mvn -N wrapper:wrapper`, commit the wrapper. `.gitignore` already has the `!.mvn/wrapper/maven-wrapper.jar` negation, so the project anticipated this and simply never did it. *(#11)*
3. One shared `resolve_port()`; apply to `xai_narrative/fatigue_xai/api.py`. *(#2, #3)*
4. Unify the four `NARRATIVE_LLM` defaults on `none`. *(#10)*
5. Fix `README.md:188` (32 → 42). *(#9)*
6. A `.github/workflows/ci.yml` running all four suites. *(#13)*

**Do second (design changes, defensible in a viva)**
7. Propagate `mode` + `signal_status` through `FrameTelemetry` and annotate narratives in stub mode. *(#7 — the biggest honesty gap)*
8. One source of truth for narrative error reasons; make Python emit canonical tokens. *(#6)*
9. Evict oldest **non-final** narrative event under queue pressure. *(#8)*
10. `gateway.max-sessions` + inbound frame rate limit. *(#5)*

**Do third (or document as deliberate limitations)**
11. Server-issued unguessable session ids; or a signed connect token. *(#4)*
12. Install Docker, actually run `docker compose up`, and capture the output as evidence. *(#12)*
13. Add `models/README.md`: no weights ship, weights are mounted, the inference path is unverified, and the model is not the research architecture. *(#1)*

**Never do:** commit a training dataset or model weights (`.gitignore` already prevents this), store driver video (the design correctly never persists frames — preserve that), or let the LLM become authoritative.

---

## 8. Overall assessment

**This is a well-engineered project, and it is under-sold by its own documentation, not over-sold.**

The genuinely strong decisions, each defensible in a viva:
- **drop-don't-queue** with a single in-flight gate and *no* queue anywhere in the pipeline;
- the **narrative path cannot break telemetry** (fire-and-forget, throttled errors, socket stays open);
- **the LLM is structurally non-authoritative** — rule engine for facts, rule-based guard (not a second LLM), deterministic template fallback;
- **`VISION_FALLBACK_TO_STUB=false`** — a misconfigured deployment refuses to serve synthetic data rather than faking it;
- **per-metric provenance** rendered in the UI instead of silently defaulted;
- a **real** frontend (9 components + a tested state machine + bounded buffers), not a placeholder dashboard;
- **183 passing tests** across four stacks, with four suites that actually exercise the failure paths (connection refused, malformed events, slow consumers, stale sockets).

The gap between that quality and the current state is almost entirely in **verification and honesty plumbing**, and less of it is a documentation problem than it first appears. The project already declares, in its own README, that the deployed model is not the research architecture (`README.md:254`) and that one test is skipped without a model (`:191`); `.gitignore` already states that weights are mounted, not committed; test counts in `README_RUN.md` and `ENGINEERING_REPORT.md` match measured reality. **The documentation is more honest than the code's behaviour in two specific places** — narrative error reasons that do not match the declared type (#6), and provenance that stops one hop short of the layer that writes sentences about a human being (#7).

What remains genuinely unfixed is narrow and tractable: **nothing has been run end-to-end, the model does not exist on this machine, the two Python services have drifted apart on port handling, and the narrative layer cannot tell synthetic data from real.** None of those are architectural failures — the architecture is the strongest part of the project. Fixing #1–#6 would move this from "a project with good bones and a handful of unchecked claims" to "a project whose every claim is checkable", which is exactly what a technical evaluator rewards.
