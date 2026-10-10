"""Frame decoding: base64 payload -> RGB pixel array.

This is the first validation boundary. Everything downstream assumes a
well-formed HxWx3 uint8 RGB array, so the checks live here and nowhere else.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
from typing import Any, Protocol

log = logging.getLogger("vision_service.decoding")

#: Reject payloads larger than this before decoding. The gateway already caps
#: frames at 512 KB by default; this is the service's own backstop so it cannot
#: be driven into a large allocation by a caller that bypasses the gateway.
DEFAULT_MAX_BASE64_BYTES = 4 * 1024 * 1024


class ImageDecodeError(ValueError):
    """The payload could not be turned into an image.

    This is NOT a server error: a blank or corrupt frame is normal input from a
    camera that lost its feed, and is answered with ``face_detected: false``.
    """


class FrameDecoder(Protocol):
    """Turns an encoded frame into pixels. Implementations must be stateless."""

    name: str

    def decode(self, frame_b64: str) -> Any:
        ...


class PillowFrameDecoder:
    """base64 JPEG/PNG -> HxWx3 RGB uint8, with explicit validation."""

    name = "pillow"

    #: Formats the pipeline is willing to accept. A driver-monitoring camera sends
    #: JPEG; PNG is tolerated for tests and screenshots.
    allowed_formats = frozenset({"JPEG", "MPO", "PNG"})

    def __init__(self, max_base64_bytes: int = DEFAULT_MAX_BASE64_BYTES) -> None:
        self.max_base64_bytes = max_base64_bytes

    def decode(self, frame_b64: str) -> Any:
        if not frame_b64:
            raise ImageDecodeError("empty frame")

        # Strip a data: URL prefix if a client forgot to. The contract says not to
        # send one, but tolerating it is cheaper than rejecting a usable frame.
        payload = frame_b64.strip()
        if payload.startswith("data:"):
            comma = payload.find(",")
            if comma == -1:
                raise ImageDecodeError("data: URL without a comma separator")
            payload = payload[comma + 1:]

        if len(payload) > self.max_base64_bytes:
            raise ImageDecodeError(
                f"frame payload of {len(payload)} chars exceeds the "
                f"{self.max_base64_bytes} char limit"
            )

        try:
            raw = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as e:
            raise ImageDecodeError(f"invalid base64: {e}") from e
        if not raw:
            raise ImageDecodeError("decoded payload is empty")

        try:
            from PIL import Image
            import numpy as np
        except ImportError as e:  # pragma: no cover - pillow is in requirements
            raise ImageDecodeError(f"image decoding is unavailable: {e}") from e

        # Pillow does the format sniffing, so an oversized-but-valid header cannot
        # masquerade as an image. Decompression bombs are bounded by Pillow itself.
        try:
            with Image.open(io.BytesIO(raw)) as img:
                if img.format not in self.allowed_formats:
                    raise ImageDecodeError(
                        f"unsupported image format {img.format!r}; expected "
                        f"{sorted(self.allowed_formats)}"
                    )
                rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
        except ImageDecodeError:
            raise
        except Exception as e:
            raise ImageDecodeError(f"could not decode image: {type(e).__name__}") from e

        if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.size == 0:
            raise ImageDecodeError(f"decoded frame has an unexpected shape {rgb.shape}")
        return rgb


def decode_base64_image(frame_b64: str) -> Any:
    """Backward-compatible module-level helper backed by :class:`PillowFrameDecoder`."""
    return PillowFrameDecoder().decode(frame_b64)
