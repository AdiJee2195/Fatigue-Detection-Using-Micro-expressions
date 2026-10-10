# Engineering report

Pre-attentive cognitive fatigue detection — provenance-aware AI, Groq integration,
session hardening, dashboard, and end-to-end verification.

Every result below was produced by a command executed in this workspace during this
pass, on 2026-10-10. Anything that could not be executed is marked **NOT VERIFIED**
rather than described as complete.

---

## 1. Executive summary

The platform is now a **real-time driver fatigue intelligence and explainability
system** rather than a monitoring demo. Four things changed in substance:

1. **A real LLM provider.** Groq is implemented behind the existing provider
   interface, server-side, with a bounded request, a classified error taxonomy, a
   factual guard, and a deterministic fallback. Narratives identify their own source
   (`groq:<model>` / `none configured`) and why they were produced (`ok`,
   `unavailable`, `skipped`, `guard_rejected`, `disabled`).
2. **Provenance that survives the whole pipeline.** `mode` and `signal_status` now
   travel from the vision service through the gateway DTO, the XAI schema, the facts
   view and the guard to the sentence on screen. A synthetic run is marked as a
   simulation *in code* on every narrative path, and the guard rejects wording that
   claims a real driver was observed.
3. **Session hardening.** A duplicate session id is refused instead of silently
   evicting the incumbent; there is a configurable session ceiling; teardown is
   guarded by socket ownership so rejecting an intruder cannot deregister the
   legitimate session; and the XAI service refuses to start on an unusable port
   instead of binding to a random one.
4. **The dashboard exists and is honest.** A complete React/TypeScript console with a
   connection state machine, provenance badges on every value, provider status, a
   bounded timeline, and diagnostics that reconcile browser counters against the
   gateway's. Nothing on screen is fabricated: unavailable values show as
   unavailable.

Verification is summarized in §6: **405 tests pass** (63 gateway, 54 vision, 229
XAI, 59 frontend), TypeScript is clean, the production build succeeds, and the
end-to-end smoke test passes **11/11** against the live four-process stack. The
dashboard was then opened in a browser against that stack and inspected.

The Groq integration was then exercised **live** with a real API key
(`openai/gpt-oss-20b`): the provider smoke test passed all four checks, and a full
stack run published Groq-authored, guard-passing explanations to the gateway — with
classified fallbacks when the provider returned no text. §3.4 and §6 record the
details, including the token-budget caveat that run exposed.

One defect was found **by running the UI** and fixed: a duplicate-session close was
reported as an origin rejection and treated as permanent, which left the dashboard in
an error state after a React StrictMode remount. §2.4 describes the fix.

---

## 2. Architecture changes

### 2.1 Provenance from sensor to sentence

Before this pass the narrative engine could not see `mode` or `signal_status`, so the
dashboard knew a measurement was synthetic while the LLM's prose could describe a
driver being observed. The fix is a threaded contract, not a UI patch:

```
vision telemetry (mode, signal_status)
   └─▶ gateway InferenceResult → TelemetryFrame → envelope payload   (relayed verbatim)
        └─▶ XAI FrameTelemetry → Assessment.provenance → facts_view
             └─▶ prompt (data_class stated to the model)
                  └─▶ guard (rejects person-claiming wording on synthetic data)
                       └─▶ compose() prepends "SIMULATION (synthetic telemetry, not a real driver)"
```

`compose()` owns the simulation marker, so it is present on the template path, the
LLM path and every guard fallback alike, and no prompt, model or injection can
suppress it (`test_the_marker_is_on_every_narrative_path`,
`test_a_model_cannot_suppress_the_marker`). Unrecognised provenance degrades to
`unknown`, never to `live`.

### 2.2 LLM failure cannot become detection failure

The rule engine computes risk level, trend, findings and recommended action. The
provider only phrases them. A provider outage yields `source: template_fallback`,
`llm_status: unavailable`, a classified `error_kind`, and a template narrative; the
assessment stream is untouched. `CRITICAL` and `UNKNOWN` never call the provider at
all (`llm_status: skipped`, `bypass_reason: critical_or_unknown_risk`) because a
critical verdict must not wait on a network round trip.

### 2.3 Slow-consumer narrative policy

The previous bounded queue dropped whatever was oldest, which could discard a
`narrative_final` — the only authoritative text the operator is meant to trust. The
new `NarrativeQueue` classifies events: provisional `narrative_chunk`s are
**coalesced** into the pending slot (losing inter-chunk timing, never text) and
terminal events are the last thing to be evicted, with a counter when even that
happens. The frame path still drops rather than queues; only the narrative buffer
changed, and it stayed bounded.

### 2.4 Session admission, and a defect found by running the UI

`SessionRegistry` now returns an explicit admission outcome: `ACCEPTED`,
`ACCEPTED_REPLACED_STALE`, `REJECTED_DUPLICATE`, `REJECTED_CAPACITY`. A duplicate id
is refused while the incumbent is live (`1008 session_already_active`); it may take
over only after the incumbent has been silent for
`gateway.session-takeover-grace-ms` (default 5 s). Capacity is capped at
`gateway.max-sessions` (default 32) and refused with `1013`. Teardown is keyed on
socket identity (`isOwner`), because a refused socket still fires
`afterConnectionClosed` and a cleanup keyed on the id alone would deregister the real
session — an integration test caught that.

Watching the dashboard revealed the reverse problem on the client side: **React
StrictMode mounts effects twice in development**, so the first socket is closed and
the second attempt can arrive before the gateway has processed that close. The
gateway correctly refuses it — and the frontend mapped every `1008` to
`origin_rejected` and treated it as permanent, parking the operator on an error card
and telling them to edit `GATEWAY_ALLOWED_ORIGINS`. An origin rejection never reaches
a close event at all (the handshake is refused with HTTP 403, surfacing as `1006`), so
this mapping was simply wrong. The client now reads the gateway's stable reason
string: `1008 + session_already_active` records a `session_already_active` problem and
**reconnects with the normal bounded backoff**, which self-heals; any other `1008`
stays permanent. Two tests cover both branches, and `CLIENT_CONTRACT.md` now
documents the close-code table.

### 2.5 Configuration that cannot silently lie

`fatigue_xai.config.resolve_port()` validates `XAI_PORT`/`PORT` explicitly: only bare
decimal digits, range 1–65535, `0` rejected with a message that explains why a random
port is indistinguishable from an outage for a client connecting to a fixed URL. The
entry point fails fast with exit code 2 instead of a `ValueError` traceback. The same
policy already existed in the vision service. Numeric Groq settings are validated the
same way: a malformed timeout is a configuration error, not a silent fallback.

### 2.6 One error vocabulary, three languages

Three gateway error reasons did not match the TypeScript union (`missing_type` vs
`narrative_event_missing_type`, `unknown_type` vs `unknown_narrative_event_type`, and
a prose sentence where a code belonged). The vocabulary is now declared once per
language — `fatigue_xai/errors.py`, `EnvelopeType.java`, `types.ts` — and each side
has a test asserting the full set, so a rename fails the build instead of silently
degrading the dashboard's `default` branch.

---

## 3. Groq integration

### 3.1 Provider

`GroqClient` (`xai_narrative/fatigue_xai/llm.py`) sits behind the existing
`LLMClient` protocol alongside `none`, `ollama`, `openai` (any OpenAI-compatible
endpoint) and `ScriptedLLM` (tests). It streams `POST {GROQ_BASE_URL}/chat/completions`
with `stream: true`, `temperature: 0`, a fixed seed and `max_tokens` from
configuration, using the existing `httpx` client — no new HTTP framework, no SDK.

### 3.2 Configuration

```dotenv
NARRATIVE_LLM=groq
GROQ_API_KEY=...                  # untracked .env only
GROQ_MODEL=...                    # required; deliberately NO default
GROQ_BASE_URL=https://api.groq.com/openai/v1
GROQ_TIMEOUT_SECONDS=8
GROQ_MAX_TOKENS=200
```

No model name is hard-coded: the catalogue changes and a stale default fails
confusingly instead of clearly (`test_no_model_is_hard_coded_and_no_credential_is_in_source`).
No key appears anywhere in source or tests. A missing key or model is **not** a
startup failure — the provider reports `enabled: false` with the missing variable
named, and narratives come from the template.

### 3.3 Request behaviour

1. Read configuration; validate numerics at construction (fail fast).
2. `is_configured()` → else `LLMError('not_configured')` without touching the network.
3. Receive validated facts (`build_messages`) with provenance and the instruction to
   state only those facts.
4. Send the constrained request; enforce connect/read/write/pool timeouts.
5. Classify every failure onto a fixed taxonomy and **redact the key** from every
   message before it can reach a log, `/health`, a narrative payload or the browser.
6. Bounded retry: at most one extra attempt, only for a plausibly transient kind
   (timeout, connection, 5xx, 429) **and** only before any text has been streamed, so
   a partial sentence is never restarted.
7. Validate the response shape; empty and malformed streams are provider faults, not
   `IndexError`s.
8. Pass the text through the guard (§3.5).
9. Accept it as the narrative, or fall back to the deterministic template.

### 3.4 Every failure stays visible

| Failure | `error_kind` | Result |
|---|---|---|
| No key / no model | `not_configured` | `llm_status: disabled`, template narrative; `/health` names the missing variable |
| 401 / 403 | `unauthorized` / `forbidden` | fallback; panel says the key was rejected |
| 400 / bad request | `bad_request` | fallback; not retried |
| Unknown model | `model_not_found` | fallback; panel says the model is not on the account |
| 429 | `rate_limited` | one bounded retry, then fallback |
| 5xx | `server_error` | one bounded retry, then fallback |
| DNS / connection | `connection` | one bounded retry, then fallback |
| Timeout | `timeout` | first-token (6 s) and total (12 s) deadlines enforced above the provider; fallback |
| Empty response | `empty_response` | fallback; the message names `GROQ_MAX_TOKENS`, because a reasoning model counts its chain of thought against the same cap |
| Malformed stream JSON | `malformed_response` | fallback |
| Excessively long text | – | guard: `answer longer than N words` → fallback |
| Guard rejection | – | `llm_status: guard_rejected`, deterministic text published |

Each of these is tested with `httpx.MockTransport`, so the suite needs no account,
no network and no money. `scripts/groq_smoke.py` is the separate, opt-in live check
(reachability, a real grounded generation through the guard, the guard's
independence from the provider, and classification of a bad model name); it exits
`2` when the environment is incomplete and never prints the key.

### 3.5 Guard (unchanged in spirit, extended for provenance)

Independent of the prompt, `validate()` checks the draft against the same facts it
was given: risk-level vocabulary and consistency, every number with its unit,
AU codes, trend direction, forbidden medical/advice wording, markdown, length,
"mentions a signal not in facts", every finding being reflected, and — when the
facts say the data is synthetic — wording that claims a person was observed.
Rejection is not fatal: it produces the deterministic explanation and a
`guard_rejected` status.

---

## 4. Security improvements

| Area | Change |
|---|---|
| Session takeover | Duplicate live id refused (`1008`); takeover only after a silence grace window; refusal never closes the incumbent |
| Resource limits | `gateway.max-sessions` (32) with `1013` + reason; each session's buffer is bounded (`max-frame-bytes` + envelope + 64 KiB container headroom); session counters exposed on `/health` |
| Teardown safety | Cleanup requires socket ownership (`SessionRegistry.isOwner`), so a rejected intruder cannot deregister the real session |
| Origin control | `GATEWAY_ALLOWED_ORIGINS` (localhost by default) — documented for what it is: browser-level, not authentication |
| Input validation | Two frame ceilings, JSON shape, timestamp normalisation to strictly increasing epoch seconds; malformed/oversized input costs a frame, not the session |
| Port configuration | `0`, negative, non-numeric and out-of-range ports are startup errors in both Python services |
| Credentials | Groq key server-side only; redacted from provider messages before logs, `/health`, error payloads or the UI; `.env` gitignored, `.env.example` holds placeholders |
| Privacy | Frames processed transiently, never written to disk, never logged as bytes, never sent to an LLM; only numeric facts reach the provider |
| Provenance enforcement | Synthetic data cannot be narrated as a real observation — enforced in code, not by prompt wording |

**Not implemented and not claimed:** authentication, per-operator accounts, TLS
termination, rate limiting. A session id is in the URL and is public knowledge; it is
not a substitute for authentication. See the README's security section for the
supported deployment shapes.

---

## 5. Files changed

### Gateway (Java)

- `websocket/SessionRegistry.java` — admission outcomes, cap, ownership check, idle tracking
- `websocket/VideoWebSocketHandler.java` — admission wiring, `1008 session_already_active`, capacity refusal, ownership-guarded teardown
- `config/WebSocketConfig.java` — two frame ceilings, explicit idle timeout
- `dto/InferenceResult.java`, `dto/TelemetryFrame.java` — provenance relay
- `dto/EnvelopeType.java`, `dto/NarrativeEventType.java` — canonical vocabularies
- `service/EnvelopeFactory.java` — narrative event routing and payload integrity
- `service/DownstreamHealth.java` — monotonic failure ordering (a failure in the same millisecond as a success used to compare as healthy) and three-state health (`true` failing / `false` observed-healthy / `null` nothing attempted)
- `service/OrchestrationService.java` — per-session frame orchestration and the in-flight backpressure flag
- `web/MonitoringController.java` — `/health`, `/sessions`
- `resources/application.yml` — `max-sessions`, `session-takeover-grace-ms`, limits
- Tests: `websocket/SessionAdmissionTest.java`, `dto/NarrativeErrorReasonsTest.java`, `dto/TelemetryFrameProvenanceTest.java`, plus expanded `GatewayWebSocketIntegrationTest`, `EnvelopeFactoryTest`, `DownstreamHealthTest`, `OrchestrationServiceTest`, `MonitoringEndpointsTest`

### Vision (Python)

- `app.py` — mode/fallback policy, degraded status, port resolution
- `telemetry.py` — `mode`, `mode_class`, `signal_status` on every row; `null` for unmeasured values
- `model.py`, `pipeline.py`, `face.py`, `features.py`, `decoding.py`, `stub.py`, `status.py` — explicit model states, provenance vocabulary, no fabricated measurements
- `tests/test_app.py` — 54 tests including missing model, degraded/fallback policy, provenance, null telemetry

### XAI narrative engine (Python)

- `llm.py` — `GroqClient`, error taxonomy, bounded retry, redaction, `ProviderStatus`
- `config.py` — validated port/host resolution
- `errors.py` — canonical error reasons
- `events.py` — `NarrativeQueue` (coalesce provisional, preserve terminal, bounded)
- `narrative.py` — provider selection, critical bypass, source/status reporting, timings
- `guard.py`, `prompt.py` — grounded prompt + independent validation incl. synthetic-data wording
- `api.py` — `/health` provider status, WebSocket error contract, preview/session REST
- `schemas.py`, `session.py`, `rules.py` — provenance on assessments
- Tests: `test_groq_provider.py`, `test_provenance.py`, `test_error_contract.py`, `test_event_queue.py`, `test_config.py`, plus expanded `test_narrative.py`, `test_service.py`, `test_schemas_guard.py`

### Frontend (TypeScript/React)

- `lib/FatigueSession.ts` — connection state machine, backpressure, bounded buffers, provider/error state, **duplicate-session handling fixed**
- `lib/backoff.ts`, `lib/frameSource.ts`, `lib/gatewayApi.ts`, `lib/hooks.ts`, `lib/useFatigueSession.ts`
- `types.ts` — contract mirror incl. `LlmStatus`, `LlmErrorKind`, provenance vocabularies
- `components/` — `StatusHeader`, `RiskPanel`, `TelemetryPanel`, `RiskTimeline`, `ExplainabilityPanel`, `NarrativePanel`, `DiagnosticsPanel`, `CameraPanel`, `Disclaimer`, `primitives`
- Tests: `FatigueSession.test.ts` (state machine incl. both `1008` branches), `provenanceAndProvider.test.ts`, `errorContract.test.ts`, `lib.test.ts`

### Docker

- `docker-compose.yml` — five services (`frontend`, `gateway`, `vision`, `narrative-xai`, `ollama` behind the `llm` profile), service-DNS URLs, healthchecks, `depends_on` for the gateway, limits passed through
- `frontend/Dockerfile`, `frontend/nginx.conf`, `gateway/Dockerfile`, `vision_service/Dockerfile`, `xai_narrative/Dockerfile` — non-root, healthchecked

### Documentation

- `README.md` — product framing, differentiators, data flow, Groq setup, security model, performance, design decisions, troubleshooting, limitations
- `CLIENT_CONTRACT.md` — provenance fields, two frame ceilings, backoff policy, close-code table
- `.env.example` — every variable with safe defaults, Groq block, both frame ceilings, `PORT` footgun
- `ENGINEERING_REPORT.md` (this file), `scripts/groq_smoke.py`, `scripts/smoke_test.py`

---

## 6. Verification results

Commands were run in this workspace on 2026-10-10. Filtered pipelines preserve the
tested command's status (`pipefail` / explicit `PIPESTATUS`).

| Verification | Result | Evidence |
|---|---|---|
| Gateway tests | **63 passed, 0 failures** | `cd gateway && mvn -o -B test` → `Tests run: 63, Failures: 0, Errors: 0, Skipped: 0`, `BUILD SUCCESS` (Maven 3.9.9; `mvn` is not on `PATH` here, so the distribution fetched to `/tmp/mvn` was used) |
| Vision tests | **54 passed** | `.venv/bin/python -m pytest vision_service/tests -q` → `54 passed in 0.61s` |
| XAI tests | **229 passed, 5 skipped** | `.venv/bin/python -m pytest xai_narrative/tests -q` → `229 passed, 5 skipped in 15.18s` (skips need `FATIGUE_ONNX`) |
| Frontend tests | **59 passed** | `cd frontend && npx vitest run` → `Test Files 4 passed`, `Tests 59 passed` |
| TypeScript check | **0 errors** | `npm run typecheck` → `tsc --noEmit` clean (strict, `noUncheckedIndexedAccess`) |
| Production build | **PASS, 7.34 s** | `npm run build` → `index 57.15 kB`, `charts 450.68 kB`, `css 15.85 kB` |
| Docker Compose | **YAML parses; execution NOT VERIFIED** | `/usr/bin/python3` + `yaml.safe_load` → 5 services, healthchecks on gateway/vision/narrative-xai, gateway `depends_on` both Python services, `ollama` behind the `llm` profile. `docker` is not installed, so `docker compose config/build/up` could not run |
| Synthetic E2E | **PASS 11/11** | `.venv/bin/python scripts/smoke_test.py` against the live stack (see below) |
| Browser dashboard | **verified live** | the built dev server opened against the running stack; snapshot described below |
| Real ONNX inference | **NOT VERIFIED** | no model file available; ONNX path exercised with a stub classifier plus the real preprocessing/softmax; missing-model → `degraded` + 503 is verified |
| Live Groq API | **PASSED** (2026-10-10, `openai/gpt-oss-20b`) | `scripts/groq_smoke.py` → reachability, generation (`source=llm`, guard passed), guard interlock and error classification all PASS, exit 0. Full stack with `NARRATIVE_LLM=groq`: 2 of 4 narratives `source=llm` / `llm_status=ok` (1.6 s, 2.4 s), the rest classified `empty_response` and fell back; provider `calls=5 failures=2`. Invalid key → real HTTP 401, `error_kind=unauthorized`, assessment unchanged, deterministic narrative published. The key was supplied at runtime from an untracked temp file and was never written into the project |

### End-to-end run

```
CONNECT         PASS  handshake with ws://localhost:8080/ws/video/smoke-1
FRAME SEND      PASS  1200 valid frames in 81.9s (14.6 fps)
ENVELOPE        PASS  1253 messages, all with the four envelope fields
TELEMETRY       PASS  1202 rows; mode=stub p_fatigue=0.141 face_detected=True
NARRATIVE       PASS  10 narrative events; sources=['template', ...]
ASSESSMENT      PASS  41 assessments; levels seen: ['HIGH', 'MODERATE']
BACKPRESSURE    PASS  received=1287 processed=1202 dropped=83 unparseable=2
                      vision_errors=0 unaccounted=0 — drop-don't-queue observed
BURST           PASS  80 frames sent in 8 ms (vs 5333 ms paced)
ERROR HANDLING  PASS  malformed/empty/oversized frames rejected; session survived
HARD LIMIT      PASS  container closed the connection above its ceiling
DISCONNECT      PASS  session 'smoke-1' released by the gateway
E2E RESULT: PASS
```

`unaccounted=0` is the load-bearing number: every frame the gateway received is
accounted for as processed, dropped by backpressure, unparseable or a vision error.

### Browser run

Dashboard opened at `http://localhost:5173` against the same stack. Observed:
`DEMO / SYNTHETIC` and `NO IMAGE DATA` badges; `CONNECTED`; risk **HIGH** with a 57.5 %
per-frame score and 72.2 % smoothed probability; six findings including two
`PRE-ATTENTIVE` ones (`BROW_STRAIN`, `LID_TIGHTENING`) and `PERCLOS_ELEVATED`;
per-metric provenance rendering `SYNTHETIC` for EAR/probabilities and
`NOT CONFIGURED` for rPPG; a narrative carrying
`SIMULATION (synthetic telemetry, not a real driver)` with a `fallback: template`
badge and `Explanation source: none configured`; and diagnostics where the browser's
327 frames sent matched 327 telemetry rows received, with the gateway reporting
287 processed, 0 dropped, 4 ms average and 11 ms p95 vision latency, vision `ok`
(1960 OK / 0 failed) and narrative `ok` (1 connected). Problems: *none* — the
duplicate-session fix from §2.4 self-healed the StrictMode remount.

### What the numbers do not cover

The **live Groq check** proves reachability, a grounded answer through the guard and
classified failures against the real endpoint — for one model (`openai/gpt-oss-20b`)
on one day, not for every model or model revision. The synthetic smoke test proves the
pipeline, not that the ONNX model classifies correctly. The tests prove the guard
rejects a contradicting sentence; they do not prove a model cannot produce one that
passes.

---

## 7. Remaining limitations

- **Real ONNX inference is not verified.** No model file was available. The pipeline,
  preprocessing, softmax reuse, model-state handling and the missing-model path are
  implemented and tested; classification performance on real drivers is unknown, and
  no accuracy figure is claimed anywhere.
- **Live Groq traffic is verified for one model, not for every model.** The run in §6
  used `openai/gpt-oss-20b`. Because a reasoning model streams its chain of thought on
  a channel the client ignores while those tokens still count against
  `GROQ_MAX_TOKENS`, the shipped 200 default intermittently produced no text (2 of 4
  calls); 2000 produced complete, guard-passing answers. Raise the cap or choose a
  non-reasoning model.
- **Docker was not built or run.** The compose file parses and is internally
  consistent; images are unverified in this environment.
- **EAR is only measured where MediaPipe FaceMesh is installed**, and its absolute
  values are not comparable to the bundled thresholds; the per-user relative baseline
  is what makes them usable. Real EAR was not exercised here.
- **FACS AU intensities and rPPG heart rate are not implemented**; they report
  `not_configured` / `null`, and the guard rejects narratives that mention them.
- **The classifier is a frame-level 3-class model**, not a temporal architecture; all
  temporal reasoning lives in the rule engine's windows.
- **Rules and thresholds are heuristics**, overridable via `RULES_JSON`; they are not
  clinical cut-offs.
- **Browser coverage is one Chromium build** at desktop width; Firefox/Safari and
  mobile layouts were not exercised.
- **`mvn` is not on this machine's `PATH`**; the suites were run with a local Maven
  3.9.9 distribution. On a normal developer machine `mvn test` is the command.
- **No lint task exists** for the frontend; `tsc --noEmit` under `strict` is the
  enforced static check.

---

## 8. How to run

### Mode A — synthetic mode (no keys, no model, no GPU)

```bash
cp .env.example .env                                     # every default is safe
python3 -m venv .venv
.venv/bin/pip install -r xai_narrative/requirements.txt -r vision_service/requirements.txt

VISION_MODE=stub .venv/bin/python -m vision_service.app                     # :8000
(cd xai_narrative && NARRATIVE_LLM=none ../.venv/bin/python -m fatigue_xai.api)  # :8100
(cd gateway && mvn spring-boot:run)                                         # :8080
(cd frontend && npm install && npm run dev)                                 # :5173
```

Open <http://localhost:5173>: `DEMO / SYNTHETIC`, changing risk levels, findings,
deterministic narratives and live diagnostics.

### Groq configuration

```bash
# In the untracked .env:
#   NARRATIVE_LLM=groq
#   GROQ_API_KEY=...
#   GROQ_MODEL=<a model your account supports>
#   GROQ_BASE_URL=https://api.groq.com/openai/v1
#   GROQ_TIMEOUT_SECONDS=8
#   GROQ_MAX_TOKENS=200

curl -s localhost:8100/health | python -m json.tool        # provider.enabled / model / calls / failures
.venv/bin/python scripts/groq_smoke.py                     # live check (exit 2 = env incomplete)
```

The narrative panel then shows `Explanation source: groq:<model>` with status `OK`.
Remove or invalidate the key to watch the deterministic fallback take over without
interrupting the assessment.

### Local LLM (no account)

```bash
NARRATIVE_LLM=ollama OLLAMA_MODEL=llama3.1:8b .venv/bin/python -m fatigue_xai.api
# or any OpenAI-compatible server:
NARRATIVE_LLM=openai OPENAI_COMPAT_URL=http://localhost:8300/v1 ...
```

### Live vision

```bash
VISION_MODE=onnx FATIGUE_ONNX=/abs/path/to/model.onnx .venv/bin/python -m vision_service.app
# VISION_FALLBACK_TO_STUB=false (default) → a broken model reports degraded and 503s
```

The dashboard shows `LIVE VISION` only when the telemetry itself says `mode: "onnx"`.

### Tests

```bash
(cd gateway && mvn test)
.venv/bin/python -m pytest vision_service/tests -q
.venv/bin/python -m pytest xai_narrative/tests -q
(cd frontend && npm test && npm run typecheck && npm run build)
.venv/bin/python scripts/smoke_test.py        # needs the three services running
```

### Docker

```bash
docker compose up --build          # dashboard on http://localhost:5173
docker compose --profile llm up --build   # adds Ollama
```

---

## 9. Demonstration script

1. **Start synthetic mode** with the four commands in §8 (Mode A). Open the dashboard;
   point out `DEMO / SYNTHETIC` and `NO IMAGE DATA`.
2. **Let a session run ~60 s.** Risk escalates `LOW → MODERATE → HIGH`; the findings
   list explains *why*, marking the `PRE-ATTENTIVE` ones; the narrative stream shows
   the simulation marker and a deterministic source.
3. **Read the provenance.** Telemetry shows `SYNTHETIC` for measured values and
   `NOT CONFIGURED` for rPPG — nothing is presented as measured that was not.
4. **Configure Groq** (§8) and restart the XAI service. Within an assessment or two,
   the panel badge changes to `groq:<model>` with status `OK`.
5. **Observe a grounded explanation**: the prose matches the risk level, the numbers
   and the findings shown above it, and it still carries the simulation marker.
6. **Simulate an outage**: restart with an invalid key (or `GROQ_BASE_URL` pointing at
   a dead host). The panel reads `Unavailable` with a plain-language reason and a
   `fallback: template` badge — **while telemetry and the risk level keep updating**.
   `scripts/groq_smoke.py` names the same failure as `unauthorized` / `connection`.
7. **Show the drop-don't-queue evidence**: `curl -s localhost:8080/sessions`, or run
   `scripts/smoke_test.py` and read the `BACKPRESSURE` line —
   `unaccounted=0` proves nothing was silently lost.
8. **Show the latency story**: diagnostics lists average and p95 vision latency,
   effective processed rate, dropped frames as a percentage, and the gateway's own
   view of the vision and narrative services.
9. **Stop the session** and confirm the session disappears from `/sessions` and the
   narrative bridge is closed in the gateway log.
