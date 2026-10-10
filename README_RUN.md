# Running the full pipeline

Real-time **pre-attentive** cognitive-fatigue detection: four processes, started in this order,
each in its own terminal. Nothing here needs a GPU or a database.

```
React client ──WS (base64 JPEG)──▶ Java gateway :8080
                                      │  POST /api/v1/analyze  (2 s timeout, drop-if-busy)
                                      ▼
                              Vision service :8000     (class_probs, EAR, AU, p_fatigue)
                                      │  telemetry per frame, over a long-lived WS
                                      ▼
                              Narrative XAI :8100      (FACS rules → Assessment → guarded LLM)
                                      │  assessment / narrative_* events
                                      ▼
                              gateway wraps each event in an envelope ──▶ React client
```

## 1. Start the pieces

### (Optional) Local LLM

Only needed for LLM-authored narratives. Without it the narrator emits the deterministic
template (`source: "template"`), which is a valid and fully functional mode.

```bash
ollama serve
ollama pull llama3.1:8b
```

### Narrative XAI service — `:8100`

```bash
cd xai_narrative
pip install -r requirements.txt
NARRATIVE_LLM=none python -m fatigue_xai.api          # template-only, no LLM required
# or, with Ollama running:
# NARRATIVE_LLM=ollama OLLAMA_MODEL=llama3.1:8b python -m fatigue_xai.api
```

### Vision service — `:8000`

```bash
cd <repo root>
python -m venv .venv && source .venv/bin/activate
pip install -r vision_service/requirements.txt        # or just fastapi uvicorn pydantic numpy for stub mode
PYTHONPATH=vision_service VISION_MODE=stub uvicorn vision_service.app:app --port 8000
```

`VISION_MODE=stub` needs no ONNX runtime and no model — it produces a deterministic fatigue
drift so the whole pipeline is demoable. For the real classifier use
`VISION_MODE=onnx FATIGUE_ONNX=<path>/efficientnet_fatigue.onnx` (see `vision_service/README.md`).

### Java gateway — `:8080`

```bash
cd gateway
mvn spring-boot:run
```

### React dashboard — `:5173`

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173
```

It opens in **demo mode** (no camera, no model) and shows a **DEMO / SYNTHETIC** badge.
Click *Start camera* to stream real frames; the badge only becomes **LIVE VISION** when the
telemetry itself reports `mode: "onnx"`.

To point it at another gateway without a rebuild:
`http://localhost:5173/?gateway=ws://other-host:8080&session=operator-042`.

The client implementation lives in `frontend/src/` — see `CLIENT_CONTRACT.md` for the wire
contract and `frontend/src/lib/FatigueSession.ts` for the connection state machine.

## 2. Verify it works

```bash
cd <repo root>
.venv/bin/pip install websockets pillow
.venv/bin/python scripts/smoke_test.py --url ws://localhost:8080/ws/video/smoke-1
```

It sends 1200 frames at 15 fps, probes the malformed / empty / oversized / burst cases, then
prints a **PASS/FAIL line per check** and exits non-zero unless every required check passed:

```
CONNECT         PASS  ...
TELEMETRY       PASS  1207 rows; mode=stub ...
NARRATIVE       PASS  12 narrative events ...
BACKPRESSURE    PASS  received=1287 processed=1207 dropped=78 unaccounted=0
```

Expect `telemetry` immediately, then `assessment`, then `narrative_start` → `narrative_chunk`
→ `narrative_final` once the rule engine has seen enough telemetry. Give it the full run: the
engine learns a ~60 s per-user baseline and the stub drifts on its own schedule.

Quick telemetry-only check:

```bash
.venv/bin/python scripts/smoke_test.py --seconds 20 --require-narrative false
```

`BACKPRESSURE` is checked against the gateway's own `GET /sessions` counters, because dropped
frames produce no message by design and the client therefore cannot see them. The property
tested is `received == processed + dropped + unparseable + visionErrors`: nothing is lost
silently.

## 3. Ports

| Process | Port |
|---|---|
| React dev client | 5173 (Vite default; 3000 also allowed by `GATEWAY_ALLOWED_ORIGINS`) |
| Java gateway | 8080 |
| Vision service | 8000 |
| Narrative XAI (WS + REST) | 8100 |
| Narrative XAI (MCP, HTTP transport) | 8200 |
| Ollama | 11434 |

Note: if you run a vLLM/llama.cpp OpenAI-compatible server, it usually defaults to `:8000`
too — put it elsewhere (e.g. `8300`) and set `OPENAI_COMPAT_URL=http://localhost:8300/v1`.

## 4. Containerised

```bash
docker compose up --build              # gateway + vision + narrative (template-only)
docker compose --profile llm up --build  # adds Ollama
```

## 5. Tests

```bash
cd gateway && mvn test                            # gateway: 36 tests (unit + integration + WS E2E)
.venv/bin/python -m pytest vision_service/tests -q   # vision: 42 tests (no model needed)
.venv/bin/python -m pytest xai_narrative/tests -q    # XAI: 63 tests, 1 skipped without a model
cd frontend && npm test && npm run typecheck && npm run build   # frontend: 42 tests + build
```

Plus the end-to-end smoke test above, which is the only check that exercises all four
processes together.

## 6. What each service owns

Keeping these boundaries is what makes the explanations trustworthy:

- **Vision service** produces *numbers* (probabilities, EAR, AU). It never writes prose.
- **Rule engine** (`xai_narrative/fatigue_xai/rules.py`) produces *facts*: risk level, trend,
  findings, recommended action, with every threshold explicit and tunable.
- **LLM** only *phrases* findings it was given. `guard.py` checks its output against the facts
  and a deterministic template replaces anything that fails, so a bad generation costs fluency,
  never accuracy. CRITICAL alerts skip the LLM entirely.
- **Gateway** transports and enforces backpressure. It does not reinterpret risk.

## 7. Honest limitations — read before reporting results

1. **Thresholds are starting heuristics, not validated cut-offs.** Calibrate on DROZY /
   UTA-RLDD / NTHU-DDD: `xai_narrative/rules.default.json` can be edited and loaded with
   `RULES_JSON=`. PERCLOS 15/30 % and the 0.5/1.0 s closure limits follow common practice;
   AU4/AU7/variance/blink-rate limits are placeholders.
2. **`xai_narrative/eval/` results are on synthetic telemetry.** They test pipeline logic
   (no false alarms on a steady driver, warning before the microsleep, the guard catching
   injected errors), **not** detection accuracy.
3. **The LLM path was only tested against mock servers.** Run
   `python eval/run_eval.py --ollama` locally and report those numbers.
4. **AU intensities and rPPG are not implemented; EAR is implemented conditionally.** With
   MediaPipe FaceMesh installed, EAR is measured from real landmarks (6-point formula) — but
   MediaPipe's absolute EAR values are **not comparable** to `rules.default.json`'s thresholds,
   which assume a different landmark scheme; the engine's *relative* per-user baseline is what
   makes them usable. FACS action units need a dedicated AU regressor and rPPG needs a
   multi-second window with motion/illumination compensation: both are absent, and telemetry
   reports them as `not_configured` / `null` rather than estimating them.
   Consequence: on real video the "pre-attentive" claim is **not yet demonstrated**. The stub
   reproduces the *ordering* of the signals, not real measurements.
5. **The current ONNX model is a 3-class frame classifier** (microsleep / neutral / yawning),
   not the TimeSformer/3D-CNN in the literature review. Temporal reasoning lives in the rule
   engine's windows.
6. **Drop-if-busy means achieved frame rate = 1 / vision latency.** The rule engine's blink and
   PERCLOS logic assumes roughly 15-24 fps; watch `effective_fps` in the gateway's periodic
   `METRICS` log line, and raise `gap_reset_s` in the rules JSON if your vision latency is high.
7. **The narrative WebSocket is unauthenticated** and origins default to localhost. Restrict
   `GATEWAY_ALLOWED_ORIGINS` and add authentication before any real deployment.
