"""Model inference: face crop -> class logits.

Wraps the exported classifier behind an interface so a different architecture can
be dropped in without touching the pipeline.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional, Protocol, Sequence

from .paths import ensure_xai_narrative_importable

log = logging.getLogger("vision_service.model")

#: Class order exported by efficientnet_fatigue.onnx (see fatigue_xai/cv_adapter.py).
LABELS = ("microsleep", "neutral", "yawning")

#: Input/output names the ONNX graph is expected to expose.
EXPECTED_OUTPUT = "class_logits"
EXPECTED_INPUT_SHAPE = (3, 224, 224)


class ModelUnavailableError(RuntimeError):
    """The classifier cannot be used, and the pipeline must not pretend otherwise.

    Raised for a missing file, a missing runtime, an unreadable graph, or an input
    signature the preprocessor cannot satisfy. The API turns this into an explicit
    service error rather than silently serving synthetic data.
    """


class FatigueClassifier(Protocol):
    """Frame-level fatigue classifier."""

    name: str

    def predict(self, crop_rgb: Any) -> Sequence[float]:
        """Return raw logits in :data:`LABELS` order."""
        ...


class OnnxFatigueClassifier:
    """EfficientNet-B0 frame classifier exported to ONNX.

    Honest scope: this is a **frame-level 3-class classifier**
    (microsleep / neutral / yawning), not the TimeSformer or 3D-CNN architecture
    discussed in the literature review. It has no temporal receptive field of its
    own — all temporal reasoning in this system lives in the rule engine's sliding
    windows. Do not describe the deployed model as the research architecture.

    The model file and its ``.onnx.data`` sidecar must sit in the same directory.
    """

    name = "onnx-efficientnet-b0"

    def __init__(self, path: str, output_name: str = EXPECTED_OUTPUT) -> None:
        self.path = path
        if not path:
            raise ModelUnavailableError(
                "no model path configured; set FATIGUE_ONNX to the exported .onnx file"
            )
        if not os.path.isfile(path):
            raise ModelUnavailableError(f"ONNX model not found at {path!r}")

        sidecar = path + ".data"
        if not os.path.isfile(sidecar):
            # The export ships weights separately for larger graphs; the runtime
            # needs both files together.
            log.warning("no %s alongside the model — the runtime may fail to load it",
                        os.path.basename(sidecar))

        try:
            import onnxruntime as ort
        except ImportError as e:
            raise ModelUnavailableError(f"onnxruntime is not installed: {e}") from e

        try:
            self._session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        except Exception as e:
            raise ModelUnavailableError(f"could not load the ONNX graph: {e}") from e

        inputs = self._session.get_inputs()
        if not inputs:
            raise ModelUnavailableError("the ONNX graph exposes no inputs")
        self.input_name = inputs[0].name
        self._validate_input(inputs[0])

        available = {output.name for output in self._session.get_outputs()}
        if output_name not in available:
            raise ModelUnavailableError(
                f"the graph has no {output_name!r} output; found {sorted(available)}"
            )
        self.output_name = output_name

        # Reuse the narrative layer's preprocessing so the softmax / p_fatigue
        # definition and the ImageNet normalisation exist in exactly one place.
        ensure_xai_narrative_importable()
        try:
            from fatigue_xai.cv_adapter import preprocess_face_crop
        except ImportError as e:
            raise ModelUnavailableError(
                f"fatigue_xai.cv_adapter is required for preprocessing: {e}"
            ) from e
        self._preprocess: Callable[[Any], Any] = preprocess_face_crop

    def _validate_input(self, tensor: Any) -> None:
        shape = list(tensor.shape)
        # Dynamic axes come through as strings (e.g. 'batch'); only the static
        # parts are checked.
        if len(shape) != 4:
            raise ModelUnavailableError(
                f"expected a 4-D NCHW input, got shape {shape} for input {tensor.name!r}"
            )
        channels, height, width = shape[1], shape[2], shape[3]
        if channels not in (EXPECTED_INPUT_SHAPE[0], "3"):
            raise ModelUnavailableError(f"expected 3 input channels, got {channels!r}")
        for actual, expected, axis in ((height, EXPECTED_INPUT_SHAPE[1], "height"),
                                       (width, EXPECTED_INPUT_SHAPE[2], "width")):
            if isinstance(actual, int) and actual != expected:
                raise ModelUnavailableError(
                    f"model expects a {actual}px {axis}, but preprocessing produces "
                    f"{expected}px"
                )

    def predict(self, crop_rgb: Any) -> Sequence[float]:
        tensor = self._preprocess(crop_rgb)
        outputs = self._session.run([self.output_name], {self.input_name: tensor})
        logits = outputs[0][0]
        return [float(v) for v in logits]

    def describe(self) -> str:
        return f"{self.name} ({os.path.basename(self.path)})"


def softmax(logits: Sequence[float]) -> Sequence[float]:
    """Reference softmax, used only when the classifier adapter is unavailable."""
    import math
    top = max(logits)
    exps = [math.exp(v - top) for v in logits]
    total = sum(exps) or 1.0
    return [v / total for v in exps]


def classify_to_probs(logits: Sequence[float],
                      to_telemetry: Optional[Callable[..., Any]] = None,
                      **kwargs: Any) -> Any:
    """Turn logits into a telemetry row.

    Prefers ``fatigue_xai.cv_adapter.logits_to_telemetry`` so the exported softmax
    and ``p_fatigue`` definition stay identical to the narrative layer's; falls back
    to the local softmax only if that import is unavailable.
    """
    if to_telemetry is not None:
        return to_telemetry(list(logits), **kwargs)
    probs = dict(zip(LABELS, softmax(logits)))
    return {"class_probs": probs, "p_fatigue": probs["microsleep"] + probs["yawning"]}
