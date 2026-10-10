"""Vision pipeline: base64 frame in, telemetry contract out.

This module is deliberately thin. It *composes* the stages and owns the one
decision that matters for honesty — what to do when the requested mode is not
available:

    decode -> locate face -> extract features -> infer -> build telemetry

Each stage lives in its own module behind an interface, so a CV component can be
replaced without touching this file:

======================  ================================  =============================
stage                   module                            swappable implementation
======================  ================================  =============================
decode                  :mod:`vision_service.decoding`    ``PillowFrameDecoder``
face location           :mod:`vision_service.face`        MediaPipe -> Haar -> centre crop
EAR / AU / rPPG         :mod:`vision_service.features`    provider protocols
inference               :mod:`vision_service.model`       ``OnnxFatigueClassifier``
telemetry               :mod:`vision_service.telemetry`   contract builder
synthetic drive         :mod:`vision_service.stub`        demo mode only
======================  ================================  =============================

Modes (``VISION_MODE``)
-----------------------
``stub`` (default)
    No model and no image decoding. A deterministic sawtooth drive produces
    plausible class probabilities, EAR droop and FACS AU intensities so the whole
    system (gateway, backpressure, rule engine, guard, narrative stream) can be
    demonstrated with no GPU, no model file and no camera. Every row is labelled
    ``mode: "stub"`` and every signal is reported as ``synthetic``.
``onnx``
    Real inference. The model is validated at load time; if it is missing or
    unusable the service **reports itself as degraded and refuses to serve
    synthetic telemetry** unless ``VISION_FALLBACK_TO_STUB=true`` is set
    explicitly. Silently substituting fabricated probabilities for a real model
    is the single worst failure mode this project could have.

Honesty rules enforced here
---------------------------
* A centre-crop fallback is never reported as a detected face, and the classifier
  is not run on it: a face classifier applied to a region that was never
  confirmed to contain a face produces a number that means nothing.
* EAR and AU intensities are never invented. With no provider wired up they are
  ``null``/``{}`` and the row says ``not_configured``; the rule engine degrades
  to the CNN-driven rules and reports which signals it had.
* ``au`` carries FACS intensities 0-5; ``au_intensities`` is the 0-1 copy for the
  dashboard gauges. Mixing the two up is the easiest way to break the engine.

Backwards-compatible re-exports at the bottom of this module keep the earlier
``from .pipeline import ...`` call sites working.
"""
from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Dict, Optional

from .decoding import (  # noqa: F401  (re-exported for backwards compatibility)
    FrameDecoder,
    ImageDecodeError,
    PillowFrameDecoder,
    decode_base64_image,
)
from .face import FaceCrop, FaceLocator, mean_luminance
from .features import (
    EarAuProvider,
    FeatureExtractor,
    MediaPipeFaceMeshEarAuProvider,
    RppgProvider,
    UnavailableEarAuProvider,
    UnavailableRppgProvider,
)
from .model import (
    FatigueClassifier,
    ModelUnavailableError,
    OnnxFatigueClassifier,
    classify_to_probs,
)
from .paths import ensure_xai_narrative_importable
from .status import FaceSource, SignalStatus
from .stub import (
    DEFAULT_CYCLE_S,
    DEFAULT_RAMP_S,
    stub_drive,  # noqa: F401  (re-exported for backwards compatibility)
    stub_signals,
)
from .telemetry import (  # noqa: F401  (re-exported for backwards compatibility)
    AU_ACTIVE_THRESHOLD,
    AU_MAX,
    LABELS,
    MODE_ONNX,
    MODE_STUB,
    NO_FACE_PROBS,
    build_response,
    no_face_response,
    signal_status,
)

log = logging.getLogger("vision_service.pipeline")

#: FACS Action Units the narrative rule engine consumes. Kept here for reference;
#: the AU map itself comes from the feature providers.
AU_CODES = ("AU4", "AU7", "AU43", "AU26")

#: Modes the service recognises. Anything else is a configuration error and is
#: refused at load time rather than quietly treated as stub.
KNOWN_MODES = (MODE_STUB, MODE_ONNX)

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _resolve_to_telemetry():
    """Return ``fatigue_xai.cv_adapter.logits_to_telemetry``, or None if unavailable.

    Importing the narrative layer's adapter keeps the softmax and ``p_fatigue``
    definition in exactly one place. When the adapter cannot be imported (no
    ``xai_narrative`` on the path) the local softmax in :mod:`vision_service.model`
    is used as a fallback so ONNX mode still works.
    """
    try:
        ensure_xai_narrative_importable()
        from fatigue_xai.cv_adapter import logits_to_telemetry
    except Exception as e:  # pragma: no cover - depends on deployment layout
        log.warning("vision: fatigue_xai.cv_adapter unavailable (%s); using the local softmax", e)
        return None
    return logits_to_telemetry


class VisionPipeline:
    """Frame -> telemetry.

    ``load()`` is called once from the app lifespan. Construction does no I/O so
    the pipeline stays trivially constructible in tests.
    """

    def __init__(self,
                 mode: Optional[str] = None,
                 model_path: Optional[str] = None,
                 seed: int = 7,
                 decoder: Optional[FrameDecoder] = None,
                 locator: Optional[FaceLocator] = None,
                 features: Optional[FeatureExtractor] = None,
                 ear_au_provider: Optional[EarAuProvider] = None,
                 rppg_provider: Optional[RppgProvider] = None,
                 classifier: Optional[FatigueClassifier] = None,
                 fallback_to_stub: Optional[bool] = None) -> None:
        requested = (mode or os.getenv("VISION_MODE", MODE_STUB)).strip().lower()
        self.requested_mode = requested
        # An unknown VISION_MODE is treated as a misconfiguration, not as stub: it
        # is surfaced through health so a typo cannot silently disable real ML.
        self.mode = requested if requested in KNOWN_MODES else MODE_STUB
        self.model_path = model_path or os.getenv("FATIGUE_ONNX", "")
        self.fallback_to_stub = (
            _env_flag("VISION_FALLBACK_TO_STUB", False) if fallback_to_stub is None else fallback_to_stub
        )

        self.decoder = decoder or PillowFrameDecoder()
        self.locator = locator or FaceLocator()
        self.features = features or FeatureExtractor(
            ear_au=ear_au_provider or MediaPipeFaceMeshEarAuProvider(),
            rppg=rppg_provider or UnavailableRppgProvider(),
        )

        self._classifier: Optional[FatigueClassifier] = classifier
        self._to_telemetry = None
        self._load_error: Optional[str] = None

        self._rng = random.Random(seed)
        self._t0 = time.time()
        self._cycle_s = float(os.getenv("STUB_CYCLE_S", str(DEFAULT_CYCLE_S)))
        self._ramp_s = float(os.getenv("STUB_RAMP_S", str(DEFAULT_RAMP_S)))

    # ------------------------------------------------------------------ state
    @property
    def model_loaded(self) -> bool:
        """True only when real inference is actually available."""
        return self._classifier is not None and self.mode == MODE_ONNX

    @property
    def face_backend(self) -> str:
        return self.locator.backend

    @property
    def load_error(self) -> Optional[str]:
        """Why the requested mode is unavailable, if it is."""
        return self._load_error

    @property
    def serve_mode(self) -> str:
        """The mode this process will actually use for the next frame."""
        return MODE_ONNX if self.model_loaded else MODE_STUB

    @property
    def degraded(self) -> bool:
        """Requested ONNX but cannot serve it — a real, reportable fault state."""
        return self.requested_mode == MODE_ONNX and not self.model_loaded

    @property
    def health_status(self) -> str:
        return "degraded" if self.degraded else "healthy"

    def describe(self) -> Dict[str, Any]:
        """Machine-readable health payload shared with ``GET /health``."""
        return {
            "status": self.health_status,
            "mode": self.serve_mode,
            "requested_mode": self.requested_mode,
            "model_loaded": self.model_loaded,
            "model_path": self.model_path or None,
            "fallback_to_stub": self.fallback_to_stub,
            "degraded": self.degraded,
            "load_error": self._load_error,
            "face_backend": self.face_backend,
            "decoder": getattr(self.decoder, "name", "unknown"),
            "signals": {
                "ear_au": getattr(self.features.ear_au, "name", "unknown"),
                "rppg": getattr(self.features.rppg, "name", "unknown"),
            },
        }

    # -------------------------------------------------------------- lifecycle
    def load(self) -> None:
        """Resolve the requested mode. Never raises; failures are recorded."""
        if self.requested_mode not in KNOWN_MODES:
            self._load_error = (
                f"unknown VISION_MODE {self.requested_mode!r}; expected one of {list(KNOWN_MODES)}"
            )
            log.error("vision: %s — serving synthetic telemetry only", self._load_error)
            self.mode = MODE_STUB
            return

        if self.requested_mode != MODE_ONNX:
            self.mode = MODE_STUB
            log.info("vision: stub mode — telemetry is SYNTHETIC (decoder %s, face backend %s)",
                     getattr(self.decoder, "name", "?"), self.face_backend)
            return

        # ONNX requested from here on.
        self._to_telemetry = self._to_telemetry or _resolve_to_telemetry()
        if self._classifier is None:
            try:
                self._classifier = OnnxFatigueClassifier(self.model_path)
            except ModelUnavailableError as e:
                self._load_error = str(e)
                self.mode = MODE_ONNX  # stays requested; model_loaded stays False
                if self.fallback_to_stub:
                    log.warning(
                        "vision: ONNX unavailable (%s) — VISION_FALLBACK_TO_STUB=true, "
                        "serving SYNTHETIC telemetry", e)
                else:
                    log.error(
                        "vision: ONNX unavailable (%s) — service is DEGRADED and will "
                        "refuse to serve synthetic data; set VISION_FALLBACK_TO_STUB=true "
                        "to allow a labelled stub fallback", e)
                return

        self.mode = MODE_ONNX
        self._load_error = None
        log.info("vision: ONNX mode — model %s, face backend %s, EAR/AU %s, rPPG %s",
                 getattr(self._classifier, "name", "?"), self.face_backend,
                 getattr(self.features.ear_au, "name", "?"),
                 getattr(self.features.rppg, "name", "?"))

    # ------------------------------------------------------------ entry point
    def analyze(self, frame_b64: str, timestamp_ms: Optional[float] = None,
                session_id: str = "default") -> Dict[str, Any]:
        """One frame -> one telemetry row.

        Raises :class:`~vision_service.model.ModelUnavailableError` when real
        inference was requested and is unavailable, so the API can answer with an
        explicit service error instead of inventing a result. A frame that cannot
        be decoded is *not* an error: it is answered with ``face_detected: false``.
        """
        started = time.perf_counter()
        ts = self._to_epoch_seconds(timestamp_ms)

        try:
            if self.model_loaded:
                body = self._analyze_onnx(frame_b64, ts, session_id)
            elif self.requested_mode == MODE_ONNX and not self.fallback_to_stub:
                raise ModelUnavailableError(
                    self._load_error or "ONNX mode was requested but no model is loaded"
                )
            else:
                body = self._analyze_stub(ts)
        except ImageDecodeError as e:
            # Normal operational input (camera lost its feed, blank frame): the
            # engine answers UNKNOWN rather than describing a face it never saw.
            log.debug("vision: undecodable frame (%s)", e)
            body = no_face_response(ts, mode=self.serve_mode, reason=str(e))
        except ModelUnavailableError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            log.warning("vision: frame analysis failed (%s) — reporting face_detected=false",
                        type(e).__name__)
            body = no_face_response(ts, mode=self.serve_mode, reason=f"analysis failed: {type(e).__name__}")

        body["inference_latency_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        return body

    @staticmethod
    def _to_epoch_seconds(timestamp_ms: Optional[float]) -> float:
        """Client timestamps are untrusted and advisory; the gateway overwrites ts."""
        if timestamp_ms is None:
            return time.time()
        try:
            value = float(timestamp_ms)
        except (TypeError, ValueError):
            return time.time()
        if value <= 0 or value != value or value > 4e12:  # <=0, NaN, or absurd
            return time.time()
        # The wire format is epoch MILLISECONDS; seconds would break windowing.
        return value / 1000.0

    # -------------------------------------------------------------------- onnx
    def _analyze_onnx(self, frame_b64: str, ts: float, session_id: str) -> Dict[str, Any]:
        rgb = self.decoder.decode(frame_b64)
        crop = self.locator.locate(rgb)

        if crop.image is None or getattr(crop.image, "size", 0) == 0:
            return no_face_response(ts, mode=MODE_ONNX, reason="empty crop", ear_status=SignalStatus.NOT_DETECTED)

        if not crop.detected:
            # A centre crop is not a detection. Running a face classifier on a
            # region that was never confirmed to contain a face would produce a
            # meaningless number, so no telemetry is derived from it.
            return no_face_response(ts, mode=MODE_ONNX,
                                    reason="no face detected (centre-crop fallback)",
                                    ear_status=SignalStatus.NOT_DETECTED)

        features = self.features.extract(crop)
        logits = self._classifier.predict(crop.image)
        probs, p_fatigue = self._probabilities(logits, ts, session_id, features, crop)
        return build_response(
            ts=ts,
            class_probs=probs,
            p_fatigue=p_fatigue,
            ear=features.ear,
            au=features.au,
            face_detected=True,
            illumination=mean_luminance(crop.image),
            mode=MODE_ONNX,
            rppg_bpm=features.rppg_bpm,
            status=signal_status(mode=MODE_ONNX,
                                 ear_status=features.ear_status,
                                 au_status=features.au_status,
                                 rppg_status=features.rppg_status,
                                 face_source=crop.source),
        )

    def _probabilities(self, logits, ts: float, session_id: str, features, crop: FaceCrop):
        """Logits -> (class_probs, p_fatigue) via the narrative layer when possible."""
        if self._to_telemetry is not None:
            row = self._to_telemetry(
                list(logits), ts=ts, session_id=session_id,
                ear=features.ear, au=features.au, rppg_bpm=features.rppg_bpm,
                face_detected=True, illumination=mean_luminance(crop.image),
            )
            return dict(row.class_probs), float(row.p_fatigue)
        projected = classify_to_probs(logits)
        return dict(projected["class_probs"]), float(projected["p_fatigue"])

    # -------------------------------------------------------------------- stub
    def _analyze_stub(self, ts: float, elapsed_s: Optional[float] = None) -> Dict[str, Any]:
        """Deterministic SYNTHETIC telemetry whose EAR/AU lead the class probabilities."""
        if elapsed_s is None:
            elapsed_s = time.time() - self._t0

        signals = stub_signals(elapsed_s, self._rng, cycle_s=self._cycle_s, ramp_s=self._ramp_s)

        return build_response(
            ts=ts,
            class_probs=signals.class_probs,
            p_fatigue=signals.p_fatigue,
            ear=signals.ear,
            au=signals.au,
            # The demo must exercise the real rule engine, so the row presents a
            # usable face — but every signal is labelled synthetic below, and no
            # frame was ever inspected.
            face_detected=True,
            illumination=0.55,
            mode=MODE_STUB,
            status=signal_status(mode=MODE_STUB,
                                 ear_status=SignalStatus.SYNTHETIC,
                                 au_status=SignalStatus.SYNTHETIC,
                                 rppg_status=SignalStatus.NOT_CONFIGURED,
                                 face_source=FaceSource.SYNTHETIC),
        )


__all__ = [
    "AU_ACTIVE_THRESHOLD",
    "AU_CODES",
    "AU_MAX",
    "DEFAULT_CYCLE_S",
    "DEFAULT_RAMP_S",
    "FaceCrop",
    "FaceLocator",
    "ImageDecodeError",
    "KNOWN_MODES",
    "LABELS",
    "MODE_ONNX",
    "MODE_STUB",
    "NO_FACE_PROBS",
    "PillowFrameDecoder",
    "VisionPipeline",
    "build_response",
    "decode_base64_image",
    "no_face_response",
    "stub_drive",
]
