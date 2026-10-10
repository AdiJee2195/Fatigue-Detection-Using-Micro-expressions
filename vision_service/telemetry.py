"""The telemetry contract: what the gateway receives for every frame.

Authoritative definition lives in ``CLIENT_CONTRACT.md`` section 4.2; keep the two
in step. Two additive fields exist so the dashboard can be honest about what it is
showing:

``mode``
    ``stub`` or ``onnx`` — which pipeline produced this row.
``signal_status``
    per-metric provenance, e.g. ``{"ear": "live", "rppg_bpm": "unavailable"}``.

Both are additive, so an older consumer that ignores them keeps working.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .status import FaceSource, SignalStatus

#: FACS action-unit intensity runs 0-5.
AU_MAX = 5.0
#: FACS "slight" activation — the threshold the UI shows as an active AU.
AU_ACTIVE_THRESHOLD = 1.0

#: Class order for the classifier's label set.
LABELS = ("microsleep", "neutral", "yawning")

#: Used when no face is available: a neutral prior, not a guess about the driver.
NO_FACE_PROBS: Dict[str, float] = {"microsleep": 0.05, "neutral": 0.90, "yawning": 0.05}

MODE_STUB = "stub"
MODE_ONNX = "onnx"

# ---------------------------------------------------------------------------
# `mode` vs `mode_class`
# ---------------------------------------------------------------------------
#
# ``mode`` is the *pipeline's identity*: which code path produced this row
# (``stub`` or ``onnx``). It is what the dashboard badges and what the
# configuration selects, so it must not be renamed.
#
# ``mode_class`` is the *epistemic* answer to "is this a measurement of a person
# or not?", which is the only question the narrative layer is allowed to care
# about. Deriving it here — where the pipeline actually knows whether it ran a
# model on a real frame — is deliberate. Deriving it downstream would mean the
# gateway or the LLM *guessing* from a mode string, and a guess is exactly how a
# synthetic number ends up described as an observation of a driver.
#
# ``serve_mode`` is ``onnx`` only while a model is loaded (see
# VisionPipeline.serve_mode), so this mapping cannot label stub output as live.
MODE_CLASS_SYNTHETIC = "synthetic"
MODE_CLASS_LIVE = "live"
MODE_CLASS_UNKNOWN = "unknown"


def mode_class(mode: str) -> str:
    """Map a pipeline mode onto the canonical provenance vocabulary."""
    if mode == MODE_STUB:
        return MODE_CLASS_SYNTHETIC
    if mode == MODE_ONNX:
        return MODE_CLASS_LIVE
    return MODE_CLASS_UNKNOWN


def signal_status(*, mode: str,
                  ear_status: SignalStatus = SignalStatus.UNAVAILABLE,
                  au_status: SignalStatus = SignalStatus.NOT_CONFIGURED,
                  rppg_status: SignalStatus = SignalStatus.NOT_CONFIGURED,
                  face_source: FaceSource = FaceSource.NONE) -> Dict[str, str]:
    """Per-metric provenance for one telemetry row."""
    return {
        # The probabilities are synthetic in demo mode and measured otherwise.
        "class_probs": (SignalStatus.SYNTHETIC if mode == MODE_STUB else SignalStatus.LIVE).value,
        "ear": ear_status.value,
        "au": au_status.value,
        "rppg_bpm": rppg_status.value,
        "face_detection": face_source.value,
    }


def build_response(*, ts: float, class_probs: Mapping[str, float], p_fatigue: float,
                   ear: Optional[float], au: Optional[Mapping[str, float]],
                   face_detected: bool, illumination: Optional[float],
                   mode: str,
                   rppg_bpm: Optional[float] = None,
                   inference_latency_ms: float = 0.0,
                   status: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Assemble one telemetry row.

    ``au`` carries FACS intensities 0-5; ``au_intensities`` is the same data
    normalised to 0-1 for dashboard gauges, so the UI never has to know about the
    FACS scale (mixing the two up is the easiest way to break the rule engine).
    """
    au_values = {str(k): float(v) for k, v in (au or {}).items()}
    return {
        "ts": round(float(ts), 3),
        "class_probs": {str(k): float(v) for k, v in class_probs.items()},
        "p_fatigue": round(float(p_fatigue), 4),
        "ear": None if ear is None else round(float(ear), 4),
        "au": {k: round(v, 3) for k, v in au_values.items()},
        "face_detected": bool(face_detected),
        "illumination": None if illumination is None else round(float(illumination), 4),
        "rppg_bpm": None if rppg_bpm is None else float(rppg_bpm),
        "risk_score": round(float(p_fatigue), 4),
        "au_intensities": {k: round(min(1.0, max(0.0, v / AU_MAX)), 3) for k, v in au_values.items()},
        "active_aus": {k: bool(v >= AU_ACTIVE_THRESHOLD) for k, v in au_values.items()},
        "inference_latency_ms": round(float(inference_latency_ms), 2),
        "mode": mode,
        "mode_class": mode_class(mode),
        "signal_status": dict(status) if status is not None else {},
    }


def no_face_response(ts: float, mode: str = MODE_STUB, latency_ms: float = 0.0,
                     ear_status: SignalStatus = SignalStatus.NOT_DETECTED,
                     reason: Optional[str] = None) -> Dict[str, Any]:
    """A well-formed answer for an unusable frame.

    Never a 5xx: a camera that lost its feed is normal operational input, and the
    engine answers UNKNOWN rather than inventing an assessment. ``reason`` is
    carried for diagnostics only.
    """
    body = build_response(
        ts=ts,
        class_probs=NO_FACE_PROBS,
        p_fatigue=NO_FACE_PROBS["microsleep"] + NO_FACE_PROBS["yawning"],
        ear=None,
        au={},
        face_detected=False,
        illumination=None,
        mode=mode,
        inference_latency_ms=latency_ms,
        status=signal_status(mode=mode, ear_status=ear_status,
                             au_status=SignalStatus.NOT_CONFIGURED,
                             rppg_status=SignalStatus.NOT_CONFIGURED,
                             face_source=FaceSource.NONE),
    )
    if reason:
        body["signal_status"] = {**body["signal_status"], "reason": reason}
    return body
