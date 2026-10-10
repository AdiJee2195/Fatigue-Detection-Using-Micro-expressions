"""Face location: RGB frame -> face crop, with the provenance of that crop.

The chain is MediaPipe -> OpenCV Haar -> centre crop, in that order of preference.
Whatever succeeds, the result records *how* it was obtained, because a centre
crop is not a detection: telemetry built from one reports
``face_detected: false`` so the rule engine answers UNKNOWN rather than
describing a face it never found.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, Sequence, Tuple

from .status import DETECTING_SOURCES, FaceSource

log = logging.getLogger("vision_service.face")

#: Fraction of the box size added as margin on each side before cropping.
PADDING = 0.15


@dataclass(frozen=True)
class FaceCrop:
    """A crop of the frame, plus how it was located."""

    image: Any
    source: FaceSource

    @property
    def detected(self) -> bool:
        """True only when a real detector found a face."""
        return self.source in DETECTING_SOURCES


def expand_and_clip(x0: float, y0: float, x1: float, y1: float,
                    width: int, height: int) -> Optional[Tuple[int, int, int, int]]:
    """Pad a box by :data:`PADDING` and clip it to the frame. None if degenerate."""
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    if x1 <= x0 or y1 <= y0:
        return None
    pad = int(PADDING * max(x1 - x0, y1 - y0))
    ax0, ay0 = max(0, x0 - pad), max(0, y0 - pad)
    ax1, ay1 = min(width, x1 + pad), min(height, y1 + pad)
    if ax1 <= ax0 or ay1 <= ay0:
        return None
    return ax0, ay0, ax1, ay1


class FaceDetector(Protocol):
    """Locates a face in an RGB frame. Must be stateless per call."""

    name: str
    source: FaceSource

    @property
    def available(self) -> bool:
        """False when the backing library is not installed."""
        ...

    def locate(self, rgb: Any) -> Optional[Tuple[float, float, float, float]]:
        """Return an (x0, y0, x1, y1) box in pixels, or None if no face was found."""
        ...


class MediaPipeFaceDetector:
    """Short-range face detector from MediaPipe. Preferred when installed."""

    name = "mediapipe"
    source = FaceSource.MEDIAPIPE

    def __init__(self, min_confidence: float = 0.5) -> None:
        self._detector = None
        try:
            import mediapipe as mp
            self._detector = mp.solutions.face_detection.FaceDetection(
                model_selection=1, min_detection_confidence=min_confidence
            )
        except Exception as e:  # pragma: no cover - optional dependency
            log.debug("mediapipe face detection unavailable: %s", e)

    @property
    def available(self) -> bool:
        return self._detector is not None

    def locate(self, rgb: Any) -> Optional[Tuple[float, float, float, float]]:
        if self._detector is None:
            return None
        height, width = rgb.shape[:2]
        result = self._detector.process(rgb)
        detections = getattr(result, "detections", None)
        if not detections:
            return None
        box = detections[0].location_data.relative_bounding_box
        return (box.xmin * width, box.ymin * height,
                (box.xmin + box.width) * width, (box.ymin + box.height) * height)


class OpenCvHaarFaceDetector:
    """Haar cascade fallback. Weaker than MediaPipe but widely available."""

    name = "opencv"
    source = FaceSource.OPENCV

    def __init__(self) -> None:
        self._cv2 = None
        self._cascade = None
        try:
            import cv2
            cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )
            if not cascade.empty():
                self._cv2, self._cascade = cv2, cascade
        except Exception as e:  # pragma: no cover - optional dependency
            log.debug("opencv face detection unavailable: %s", e)

    @property
    def available(self) -> bool:
        return self._cascade is not None

    def locate(self, rgb: Any) -> Optional[Tuple[float, float, float, float]]:
        if self._cascade is None:
            return None
        gray = self._cv2.cvtColor(rgb, self._cv2.COLOR_RGB2GRAY)
        faces = self._cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4,
                                               minSize=(48, 48))
        if len(faces) == 0:
            return None
        x, y, w, h = max(faces, key=lambda f: int(f[2]) * int(f[3]))
        return (float(x), float(y), float(x + w), float(y + h))


class FaceLocator:
    """Applies the detector chain and produces a :class:`FaceCrop`.

    The centre-crop fallback always yields *something* so the pipeline keeps
    running on a lost camera, but it is labelled and reported as undetected.
    """

    def __init__(self, detectors: Optional[Sequence[FaceDetector]] = None,
                 prefer: Optional[Sequence[str]] = None,
                 fallback_fraction: float = 0.6) -> None:
        candidates: List[FaceDetector] = list(detectors) if detectors is not None else [
            MediaPipeFaceDetector(), OpenCvHaarFaceDetector(),
        ]
        if prefer:
            order = {name.lower(): index for index, name in enumerate(prefer)}
            candidates.sort(key=lambda d: order.get(d.name.lower(), len(order)))
        self.detectors = [d for d in candidates if d.available]
        self.fallback_fraction = fallback_fraction

    @property
    def backend(self) -> str:
        """Name of the first available real detector, or 'centre'."""
        return self.detectors[0].name if self.detectors else "centre"

    def locate(self, rgb: Any) -> FaceCrop:
        for detector in self.detectors:
            try:
                box = detector.locate(rgb)
            except Exception as e:  # pragma: no cover - defensive
                log.debug("%s face detection failed: %s", detector.name, e)
                continue
            if box is None:
                continue
            height, width = rgb.shape[:2]
            clipped = expand_and_clip(*box, width=width, height=height)
            if clipped is None:
                continue
            x0, y0, x1, y1 = clipped
            crop = rgb[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            return FaceCrop(image=self._contiguous(crop), source=detector.source)

        # No detector available, or none found a face.
        return FaceCrop(image=self._centre(rgb), source=FaceSource.CENTRE_CROP)

    def _centre(self, rgb: Any) -> Any:
        height, width = rgb.shape[:2]
        side = max(1, int(min(height, width) * self.fallback_fraction))
        y0, x0 = (height - side) // 2, (width - side) // 2
        return self._contiguous(rgb[y0:y0 + side, x0:x0 + side])

    @staticmethod
    def _contiguous(array: Any) -> Any:
        import numpy as np
        return np.ascontiguousarray(array)


def mean_luminance(rgb: Any) -> Optional[float]:
    """Mean luminance of a region in 0-1, used by the engine's low-light check."""
    try:
        import numpy as np
        arr = np.asarray(rgb, dtype=np.float32)
        if arr.ndim == 3:
            arr = arr.mean(axis=2)
        return float(arr.mean() / 255.0)
    except Exception:  # pragma: no cover - defensive
        return None
