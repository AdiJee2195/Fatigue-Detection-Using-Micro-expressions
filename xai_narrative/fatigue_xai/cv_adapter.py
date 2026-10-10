"""Bridge from the existing CV stage (EfficientNet-B0 ONNX) to FrameTelemetry.

NOTE (differs from the handover wording): ``efficientnet_fatigue.onnx`` has ONE
output, ``class_logits`` [batch, 3].  Softmax and ``p_fatigue`` are NOT produced
by the graph; they are computed here.  Class order: 0=microsleep 1=neutral 2=yawning.
"""
from __future__ import annotations

import os
import time
from typing import Dict, Optional, Sequence

import numpy as np

from .schemas import LABELS_3, FrameTelemetry

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def softmax(logits: Sequence[float]) -> np.ndarray:
    z = np.asarray(logits, dtype=np.float64)
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def logits_to_telemetry(logits: Sequence[float], ts: float, session_id: str = "default", frame_id: int = 0,
                        ear: Optional[float] = None, au: Optional[Dict[str, float]] = None,
                        rppg_bpm: Optional[float] = None, face_detected: bool = True,
                        illumination: Optional[float] = None) -> FrameTelemetry:
    p = softmax(logits)
    probs = {lab: float(v) for lab, v in zip(LABELS_3, p)}
    return FrameTelemetry(session_id=session_id, frame_id=frame_id, ts=ts, class_probs=probs,
                          p_fatigue=float(p[0] + p[2]), ear=ear, au=au or {}, rppg_bpm=rppg_bpm,
                          face_detected=face_detected, illumination=illumination)


def preprocess_face_crop(rgb_uint8: np.ndarray) -> np.ndarray:
    """HxWx3 RGB uint8 face crop -> [1,3,224,224] float32 (ImageNet normalisation)."""
    from PIL import Image
    img = Image.fromarray(rgb_uint8).convert("RGB").resize((224, 224), Image.BILINEAR)
    x = (np.asarray(img, dtype=np.float32) / 255.0 - MEAN) / STD
    return np.ascontiguousarray(x.transpose(2, 0, 1)[None])


class OnnxFatigueModel:
    """Thin wrapper so the narrative stack can be tested end-to-end on real crops.
    Keep ``.onnx`` and ``.onnx.data`` in the same directory."""

    def __init__(self, path: Optional[str] = None):
        import onnxruntime as ort
        path = path or os.getenv("FATIGUE_ONNX", "models/efficientnet_fatigue.onnx")
        self.sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name  # 'face_crop'

    def infer(self, rgb_uint8: np.ndarray) -> np.ndarray:
        return self.sess.run(["class_logits"], {self.input_name: preprocess_face_crop(rgb_uint8)})[0][0]

    def telemetry(self, rgb_uint8: np.ndarray, ts: Optional[float] = None, **kw) -> FrameTelemetry:
        return logits_to_telemetry(self.infer(rgb_uint8), ts if ts is not None else time.time(), **kw)
