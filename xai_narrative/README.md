# Narrative XAI layer (RO4) — Aman

Turns the CV stage's telemetry into time-stamped **Operator Risk Assessments** in plain English,
without letting the LLM invent facts.

```
CV / EVM stage ──FrameTelemetry──▶  FACS rule engine  ──Assessment──▶  Narrator ──▶ events ──▶ gateway ──▶ UI
 (Advik, Akshat)   per frame         deterministic       facts only    LLM (Ollama)   WS JSON
                                     findings + level                  + guard
                                     + trend                           + template fallback
                                              ▲
                                              └── same engine exposed as MCP tools/resources (any MCP host)
```

**Design rule:** numbers, FACS codes, risk level, trend, and the *recommended action* are all produced by
code. The LLM only phrases pre-written finding clauses. Its output is then checked against those facts;
anything that fails is replaced by a deterministic template. A wrong statement can therefore cost fluency,
never accuracy. CRITICAL alerts skip the LLM entirely (template, ~0 ms).

## Quick start

```bash
pip install -r requirements.txt
ollama pull llama3.1:8b && ollama serve          # optional; without it you get the template narrator
python -m fatigue_xai.api                         # REST + WebSocket on :8100
python -m fatigue_xai.simulator --scenario drift_to_microsleep   # replay a synthetic drive, print narratives
python examples/mcp_client_demo.py                # talk to the MCP server over stdio
python -m pytest tests -q                         # 63 tests
python eval/run_eval.py [--ollama]                # reliability report -> eval/out/
```
Set `NARRATIVE_LLM=none` to force template-only, `openai` for vLLM. See `.env.example`.

## What each teammate needs

### Advik / Akshat — telemetry in (one JSON per frame)
```json
{"ts": 184.2, "class_probs": {"microsleep": 0.04, "neutral": 0.90, "yawning": 0.06},
 "ear": 0.27, "au": {"AU4": 2.1, "AU7": 1.4, "AU43": 0.5}, "face_detected": true, "illumination": 0.5}
```
Only `ts` + `class_probs` are required — the engine works from the current CNN alone and gets sharper as
`ear`, `au` (FACS intensity 0–5; AU4, AU7, AU43, AU26/27 are used) and `illumination` arrive.
4-class labels (`alert, early_fatigue, yawning, microsleep`) are already accepted.

> **Handover discrepancy:** `efficientnet_fatigue.onnx` exports only `class_logits`. Softmax and
> `p_fatigue` are *not* in the graph — `cv_adapter.logits_to_telemetry()` computes them
> (`p_fatigue = P(microsleep)+P(yawning)`, verified against the handover definition).

### Alok (Spring Boot) — WebSocket `ws://<host>:8100/ws/{session_id}`
Send frames as above. Receive:

| type | meaning |
|---|---|
| `assessment` | every ~2 s: level, trend, findings (for the dashboard gauges) |
| `narrative_start` | `prefix` = `"[T+00:03:04] HIGH RISK - "`, `mode` = llm/template |
| `narrative_chunk` | streamed text, **provisional** |
| `narrative_retry` | guard rejected draft; a silent retry follows |
| `narrative_final` | **authoritative** `text`, `source` = `llm` / `template` / `template_fallback`, `guard`, `latency_ms` |
| `narrative_cancelled` | pre-empted by a more severe alert |
| `error` | bad frame |

Stateless test for Postman: `POST /v1/narrative/preview {"frames":[...]}`. Also `GET /health`,
`/v1/sessions/{id}/assessment`, `/v1/sessions/{id}/narratives`.

### Aadersh (React) — text panel
Append `narrative_chunk.text` as it streams, then **replace** the panel text with `narrative_final.text`.
Show a small badge when `source != "llm"`.

### Abhishek — deployment
`Dockerfile` + `docker-compose.snippet.yml` (service + Ollama). First run: `ollama pull llama3.1:8b`.

## MCP server
`python -m fatigue_xai.mcp_server` (stdio) or `--transport http` (:8200).
Tools: `ingest_telemetry`, `ingest_telemetry_batch`, `get_operator_assessment`, `generate_risk_narrative`,
`get_narrative_history`, `get_risk_timeline`, `explain_rule`. Resources: `facs://rules/catalogue`,
`facs://prompt/system`, `session://{id}/assessment`. Prompt: `operator_risk_assessment`.
Works with MCP SDK 1.x and 2.x (tested on 1.30 and 2.2).
The real-time path (WebSocket) calls the engine in-process for latency; MCP is the interface for
supervisor-side tools/agents ("why is operator 12 at HIGH?"). Run them in one process if they must share sessions.

## Rules and guard (what is deterministic)
`fatigue_xai/rules.py` — findings: `LONG_CLOSURE, PERCLOS_ELEVATED, MODEL_MICROSLEEP, LID_DROOP, FIXED_STARE,
RAPID_BLINKING, BROW_STRAIN (AU4), LID_TIGHTENING (AU7), YAWNING (AU26/27), RISING_TREND`. Pre-attentive ones
(lid droop, fixed stare, brow/lid strain) are measured against a **per-user baseline** learned from the first
~60 s of alert-looking frames, and two of them together force at least MODERATE ("early drift") even while the CNN's
`p_fatigue` is still low. Levels use hysteresis (upgrade at once, downgrade after 5 assessments), CRITICAL needs a
closure ≥1 s, a window-mean microsleep probability ≥60%, or smoothed p ≥0.85. Face lost >30% of the window → `UNKNOWN`, not a guess. `explain_rule` prints every threshold.

`fatigue_xai/guard.py` rejects LLM text that: contains a number not in the facts (unit-aware: % vs s vs count),
cites an AU code not given, states or implies a different risk level or trend, omits a finding, gives advice,
makes medical/emotional claims, mentions pulse/heart rate without rPPG data, or uses markdown/over-length.

## Honest limitations — read before the viva
1. **Thresholds are starting heuristics, not validated cut-offs.** Calibrate on DROZY / UTA-RLDD / NTHU-DDD:
   dump `rules.default.json`, edit, load with `RULES_JSON=`. PERCLOS 15/30% and the 0.5/1.0 s closure limits follow
   common practice; AU4/AU7/variance/blink-rate limits are placeholders.
2. **`eval/` results are on synthetic telemetry** I generated, so they check pipeline logic (no false alarms on a
   steady driver, warning before the microsleep, guard catches injected errors), **not** detection accuracy.
3. **The LLM path was tested against mock Ollama/OpenAI servers and a scripted model; no real Llama run happened
   in the build environment.** Run `python eval/run_eval.py --ollama` on your machine — it reports first-attempt pass
   rate, final accept rate, p50/p95 latency and violation types — and put *those* in the paper.
4. RO4 also asks for *qualitative usability*: `eval/out/narratives_for_rating.csv` has blank 1–5 columns
   (clarity / usefulness / trust) for supervisors to fill in.
5. `EAR`, AU intensities and rPPG must still come from the EVM/optical-flow stage; until then only the CNN-driven
   rules fire.
