"""Feature extraction: face crop -> EAR, FACS AU intensities, rPPG.

Each provider declares what it can genuinely produce and reports the provenance of
every value through :class:`~vision_service.status.SignalStatus`.

The guiding rule: **never fabricate a measurement**. The project's central claim is
that eyelid droop and facial strain appear *before* a classifier's fatigue
probability rises. An invented EAR would manufacture that claim, so a provider
that cannot measure returns nothing and says so.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, Sequence, Tuple

from .face import FaceCrop
from .status import SignalStatus

log = logging.getLogger("vision_service.features")

# MediaPipe FaceMesh landmark indices for the 6-point eye model
# (Soukupova & Cech, "Real-Time Eye Blink Detection using Facial Landmarks", 2016).
LEFT_EYE = (33, 160, 158, 133, 153, 144)
RIGHT_EYE = (362, 385, 387, 263, 373, 380)


@dataclass(frozen=True)
class EarAuResult:
    """What an EAR/AU provider managed to measure for one frame."""

    ear: Optional[float] = None
    au: Dict[str, float] = field(default_factory=dict)
    ear_status: SignalStatus = SignalStatus.UNAVAILABLE
    au_status: SignalStatus = SignalStatus.NOT_CONFIGURED


@dataclass(frozen=True)
class RppgResult:
    """Heart rate from remote photoplethysmography, or an explanation of its absence."""

    bpm: Optional[float] = None
    status: SignalStatus = SignalStatus.NOT_CONFIGURED


class EarAuProvider(Protocol):
    """Measures eye openness and FACS action-unit intensities from a face crop."""

    name: str

    def extract(self, crop: FaceCrop) -> EarAuResult:
        ...


class RppgProvider(Protocol):
    """Measures heart rate from a face crop (or reports that it cannot)."""

    name: str

    def extract(self, crop: FaceCrop) -> RppgResult:
        ...


class UnavailableEarAuProvider:
    """The default: no EAR, no AU intensities, and it says so.

    Used until the optical-flow / FACS stage is wired in. The rule engine degrades
    gracefully (only CNN-driven rules fire) and reports which signals it had.
    """

    name = "unavailable"

    def extract(self, crop: FaceCrop) -> EarAuResult:
        return EarAuResult()


class MediaPipeFaceMeshEarAuProvider:
    """Real EAR from MediaPipe FaceMesh landmarks.

    Produces **EAR only**. FACS AU intensities are intentionally absent: they need a
    dedicated AU regressor (OpenFace, or a trained Action-Unit model) which is not
    part of this deployment. Reporting eye openness without AU intensities is the
    honest split, and the engine already copes with a missing ``au`` map.

    The values come from the standard 6-point eye-aspect-ratio formula

        EAR = (|p2 - p6| + |p3 - p5|) / (2 * |p1 - p4|)

    averaged over both eyes, with landmarks scaled to pixels so the ratio is not
    distorted by the frame's aspect ratio. Note that MediaPipe's absolute EAR
    values are not comparable to the thresholds in ``rules.default.json``, which
    were written for a different landmark scheme — the engine's *relative*
    per-user baseline logic is what makes them usable, and the absolute
    ``ear_closed_abs`` fallback should be recalibrated before trusting it.
    """

    name = "mediapipe-facemesh"

    def __init__(self, refine_landmarks: bool = True, min_detection_confidence: float = 0.5) -> None:
        self._mesh = None
        try:
            import mediapipe as mp
            self._mesh = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=True,
                max_num_faces=1,
                refine_landmarks=refine_landmarks,
                min_detection_confidence=min_detection_confidence,
            )
        except Exception as e:  # pragma: no cover - optional dependency
            log.debug("mediapipe face mesh unavailable: %s", e)

    @property
    def available(self) -> bool:
        return self._mesh is not None

    def extract(self, crop: FaceCrop) -> EarAuResult:
        if self._mesh is None:
            return EarAuResult(ear_status=SignalStatus.NOT_CONFIGURED)
        height, width = crop.image.shape[:2]
        try:
            result = self._mesh.process(crop.image)
        except Exception as e:  # pragma: no cover - defensive
            log.debug("face mesh processing failed: %s", e)
            return EarAuResult(ear_status=SignalStatus.NOT_DETECTED)

        faces = getattr(result, "multi_face_landmarks", None)
        if not faces:
            return EarAuResult(ear_status=SignalStatus.NOT_DETECTED)

        points = [(lm.x * width, lm.y * height) for lm in faces[0].landmark]
        left = eye_aspect_ratio(points, LEFT_EYE)
        right = eye_aspect_ratio(points, RIGHT_EYE)
        if left is None or right is None:
            return EarAuResult(ear_status=SignalStatus.NOT_DETECTED)

        return EarAuResult(
            ear=round((left + right) / 2.0, 4),
            ear_status=SignalStatus.LIVE,
            au_status=SignalStatus.NOT_CONFIGURED,
        )


def eye_aspect_ratio(points: Sequence[Tuple[float, float]],
                     indices: Sequence[int]) -> Optional[float]:
    """6-point eye aspect ratio, or None when the landmarks are degenerate."""
    try:
        p = [points[i] for i in indices]
    except IndexError:
        return None
    horizontal = math.dist(p[0], p[3])
    if horizontal <= 0:
        return None
    vertical = math.dist(p[1], p[5]) + math.dist(p[2], p[4])
    return vertical / (2.0 * horizontal)


class UnavailableRppgProvider:
    """rPPG is not implemented in this deployment.

    Remote photoplethysmography needs a multi-second window of raw frames and
    careful motion/illumination compensation; it is a research component of its
    own. Until it exists this returns ``null``, and the guard rejects any narrative
    that mentions pulse or heart rate because the fact is absent from the facts view.
    """

    name = "unavailable"

    def extract(self, crop: FaceCrop) -> RppgResult:
        return RppgResult()


@dataclass(frozen=True)
class FeatureSet:
    """All optional per-frame signals, each with its provenance."""

    ear: Optional[float]
    au: Dict[str, float]
    rppg_bpm: Optional[float]
    ear_status: SignalStatus
    au_status: SignalStatus
    rppg_status: SignalStatus

    def as_status_map(self) -> Dict[str, str]:
        return {
            "ear": self.ear_status.value,
            "au": self.au_status.value,
            "rppg_bpm": self.rppg_status.value,
        }


class FeatureExtractor:
    """Composes the EAR/AU and rPPG providers into one result."""

    def __init__(self, ear_au: Optional[EarAuProvider] = None,
                 rppg: Optional[RppgProvider] = None) -> None:
        self.ear_au = ear_au or UnavailableEarAuProvider()
        self.rppg = rppg or UnavailableRppgProvider()

    @property
    def ear_au_name(self) -> str:
        return self.ear_au.name

    @property
    def rppg_name(self) -> str:
        return self.rppg.name

    def extract(self, crop: FaceCrop) -> FeatureSet:
        ear_au = self.ear_au.extract(crop)
        rppg = self.rppg.extract(crop)
        return FeatureSet(
            ear=ear_au.ear,
            au=dict(ear_au.au),
            rppg_bpm=rppg.bpm,
            ear_status=ear_au.ear_status,
            au_status=ear_au.au_status,
            rppg_status=rppg.status,
        )
