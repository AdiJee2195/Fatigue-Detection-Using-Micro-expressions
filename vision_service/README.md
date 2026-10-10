# Vision service

Turns one base64 webcam frame into one row of **per-frame telemetry** and hands it to the
Java gateway, which forwards it both to the dashboard and to the narrative XAI engine.

It does **not** write narratives and it does **not** decide risk levels — that is
`xai_narrative/`'s job, and keeping the split sharp is what makes the explanations
auditable: this service produces numbers, the rule engine produces facts, the LLM only
phrases them.

```
React ──WS base64──▶ Gateway ──HTTP POST /api/v1/analyze──▶ Vision service
                                                                 │ telemetry JSON
                                                                 ▼
                                                       Gateway ──WS──▶ Narrative XAI
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/analyze` | one frame in, one telemetry row out |
| `GET` | `/health` | mode, whether the model loaded, which face backend is active |

### `POST /api/v1/analyze`

Request (the gateway fills in `sessionId` from the WebSocket URL):

```json
{ "frame": "<base64 JPEG, no data: prefix>", "timestamp": 1699999999123, "sessionId": "operator-001" }
```

`timestamp` is epoch **milliseconds**. Response (`ts` is epoch **seconds**):

```json
{
  "ts": 1699999999.123,
  "class_probs": {"microsleep": 0.04, "neutral": 0.90, "yawning": 0.06},
  "p_fatigue": 0.10,
  "ear": 0.27,
  "au": {"AU4": 2.1, "AU7": 1.4, "AU43": 0.5},
  "face_detected": true,
  "illumination": 0.52,
  "rppg_bpm": null,
  "risk_score": 0.10,
  "au_intensities": {"AU4": 0.42, "AU7": 0.28, "AU43": 0.10},
  "active_aus": {"AU4": true, "AU7": false, "AU43": false},
  "inference_latency_ms": 38.2
}
```

Notes on the contract:

- `class_probs` uses the model's own label set (`microsleep`, `neutral`, `yawning`) and sums to ~1.
- `au` is **FACS intensity 0-5**; `au_intensities` is the same data normalised to 0-1 for the UI gauges.
  Mixing these two scales up is the easiest way to break the rule engine's thresholds.
- `risk_score` mirrors `p_fatigue` = P(microsleep) + P(yawning).
- Unusable frames get **HTTP 200** with `face_detected: false` — not a 5xx. The gateway
  classifies 5xx as `vision_service_http_error`, and a blank frame is normal input.
- Only malformed JSON is a 422.

## Modes

`VISION_MODE` selects the pipeline.

### `stub` (default)

No model, no image decoding, no GPU. Produces a deterministic 0-1 fatigue *drive* that ramps
over ~75 s and resets every ~120 s, with plausible class probabilities, eyelid droop, brow/lid
strain and occasional yawns.

Crucially the **EAR and AU signals lead the classifier**: droop and brow strain rise from
drive ≈ 0.3 while `p_fatigue` only climbs after ≈ 0.5. That means the demo actually exercises
the pre-attentive path the project is about (`early_drift` in the rule engine) instead of a
model that is merely late.

Env: `STUB_CYCLE_S` (default 120), `STUB_RAMP_S` (default 75).

### `onnx`

Decodes the JPEG, crops the face (MediaPipe → OpenCV Haar cascade → centre crop), runs
`efficientnet_fatigue.onnx`, and projects the logits through
`fatigue_xai.cv_adapter.logits_to_telemetry` — the same function the narrative layer's tests
already cover, so softmax and the `p_fatigue` definition have exactly one implementation.

- Model path: `FATIGUE_ONNX` (default `xai_narrative/models/efficientnet_fatigue.onnx`).
  `.onnx` and `.onnx.data` must sit in the same directory.
- `xai_narrative` is put on `sys.path` automatically; override with `XAI_NARRATIVE_PATH` if
  the layout differs.
- If the model or `onnxruntime` is missing, `load()` logs a warning and **falls back to stub**
  rather than failing to start. `/health` reports the mode actually in use.

## What is deliberately NOT faked

`ear`, `au` and `rppg_bpm` come from an `EarAuProvider`. The shipped implementation is
`NullEarAuProvider`, which returns `(None, {})`. Those signals must come from the
EVM/optical-flow stage; until it exists the rule engine runs on CNN-driven rules only and
reports `signals_available: ["model_probabilities"]`. Fabricating EAR would silently
manufacture the project's central claim, so nothing here invents it.

Similarly the face cropper reports `face_detected: false` when it falls back to a centre
crop, so the narrative engine answers `UNKNOWN` ("check camera position and lighting")
instead of describing a face it never found.

## Running

```bash
# stub mode — no ML dependencies needed
cd <repo root>
pip install fastapi uvicorn pydantic numpy
PYTHONPATH=vision_service VISION_MODE=stub uvicorn vision_service.app:app --port 8000

# full dependencies + real model
pip install -r vision_service/requirements.txt
PYTHONPATH=vision_service VISION_MODE=onnx FATIGUE_ONNX=<path>.onnx uvicorn vision_service.app:app --port 8000

# tests (no model, no network)
python -m pytest vision_service/tests -q
```

`PYTHONPATH` is only needed when invoking `uvicorn` by string. Running
`python -m vision_service.app` from the repo root works without it.

## Honest limitations

1. `efficientnet_fatigue.onnx` is a **3-class frame classifier**, not the TimeSformer/3D-CNN
   described in the literature review. It sees one frame at a time, so it cannot model
   temporal evidence on its own — the temporal reasoning lives in the rule engine's windows.
2. The ONNX export does **not** contain softmax or `p_fatigue`; both are computed in Python
   (see `cv_adapter`). This is a documented discrepancy from the original handover wording.
3. EAR, AU intensities and rPPG require the EVM/optical-flow stage, which does not exist yet.
4. Face detection falls back to a centre crop when MediaPipe and OpenCV are unavailable; in
   that case every frame is reported as `face_detected: false`.
5. Stub-mode numbers are synthetic and demonstrate pipeline behaviour, **not** detection
   accuracy.
