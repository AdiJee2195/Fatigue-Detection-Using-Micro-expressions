"""FastAPI vision service: the bridge between raw frames and the narrative engine.

    POST /api/v1/analyze   one base64 frame -> telemetry contract (CLIENT_CONTRACT.md 4.2)
    GET  /health           mode, model state, face backend, signal providers, counters

Calling pattern: the gateway POSTs once per frame with a short timeout and drops
the frame if the previous call is still running, so this endpoint must stay fast
and must never hang. It answers 200 with a well-formed body for anything that
decodes to *something*; only malformed JSON is a 422.

Failure policy
--------------
``VISION_MODE=onnx`` with an unusable model is a **service fault**, not a reason to
invent results. Unless ``VISION_FALLBACK_TO_STUB=true`` is set explicitly, the
service reports ``status: "degraded"`` from ``/health`` and answers 503 from
``/api/v1/analyze``. Silent substitution of synthetic probabilities for a real
model would make every downstream claim about the system false.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from .decoding import ImageDecodeError
from .model import ModelUnavailableError
from .pipeline import VisionPipeline

SERVICE_NAME = "vision-service"
SERVICE_VERSION = "0.2.0"

log = logging.getLogger(SERVICE_NAME)


def configure_logging() -> None:
    """Console logging with a stable preamble; extra fields are appended as k=v."""
    if logging.getLogger().handlers:  # already configured (e.g. by uvicorn)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s service=" + SERVICE_NAME + " %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """One line per event, with fields appended rather than interpolated.

    Deliberately never logs frame bytes: base64 payloads are large and, for a
    driver-monitoring system, personal data. Only sizes and identifiers are logged.
    """
    detail = " ".join(f"{key}={value}" for key, value in fields.items())
    log.log(level, "event=%s %s", event, detail)


@dataclass
class ServiceStats:
    """Cheap in-process counters so /health shows whether frames are flowing."""

    frames_received: int = 0
    frames_processed: int = 0
    frames_without_face: int = 0
    decode_errors: int = 0
    model_errors: int = 0
    total_latency_ms: float = 0.0
    started_at: float = field(default_factory=time.time)

    def snapshot(self) -> Dict[str, Any]:
        avg = self.total_latency_ms / self.frames_processed if self.frames_processed else 0.0
        return {
            "frames_received": self.frames_received,
            "frames_processed": self.frames_processed,
            "frames_without_face": self.frames_without_face,
            "decode_errors": self.decode_errors,
            "model_errors": self.model_errors,
            "avg_inference_latency_ms": round(avg, 2),
            "uptime_s": round(time.time() - self.started_at, 1),
        }


class AnalyzeRequest(BaseModel):
    """What the Java gateway POSTs for every frame.

    ``timestamp`` is epoch MILLISECONDS (the browser's clock) and is advisory: the
    gateway overwrites ``ts`` downstream. ``sessionId`` is filled in by the gateway
    from the WebSocket URL and is used for logging only.
    """

    model_config = ConfigDict(extra="ignore")

    frame: str = Field(..., description="base64-encoded JPEG/PNG, no data: prefix")
    timestamp: Optional[float] = Field(None, description="epoch milliseconds at capture")
    sessionId: Optional[str] = Field(None, description="operator session id (gateway-provided)")


def create_app(pipeline: Optional[VisionPipeline] = None) -> FastAPI:
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Load the model exactly once; inference itself runs in a thread pool.
        active = pipeline or VisionPipeline()
        active.load()
        app.state.pipeline = active
        app.state.stats = ServiceStats()
        log_event("startup", **{k: v for k, v in active.describe().items()
                                if k in ("status", "mode", "requested_mode",
                                         "model_loaded", "face_backend")})
        if active.degraded:
            log_event("startup_degraded", level=logging.ERROR, reason=active.load_error or "unknown")
        yield
        log_event("shutdown")

    app = FastAPI(title="Fatigue Vision Service", version=SERVICE_VERSION, lifespan=lifespan)

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        p: VisionPipeline = app.state.pipeline
        return {
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "timestamp": round(time.time(), 3),
            **p.describe(),
            "stats": app.state.stats.snapshot(),
        }

    @app.post("/api/v1/analyze")
    async def analyze(request: AnalyzeRequest, http_request: Request) -> Dict[str, Any]:
        p: VisionPipeline = app.state.pipeline
        stats: ServiceStats = app.state.stats
        stats.frames_received += 1

        # Inference is CPU-bound: keep the event loop free so /health and
        # concurrent sessions stay responsive.
        try:
            body = await run_in_threadpool(
                p.analyze, request.frame, request.timestamp, request.sessionId or "default")
        except ModelUnavailableError as e:
            stats.model_errors += 1
            log_event("model_unavailable", level=logging.ERROR,
                      sessionId=request.sessionId or "default", reason=str(e))
            raise HTTPException(
                status_code=503,
                detail=("real inference is unavailable and synthetic fallback is disabled; "
                        f"{e}"),
            ) from e
        except ImageDecodeError as e:
            stats.decode_errors += 1
            log_event("decode_error", level=logging.WARNING,
                      sessionId=request.sessionId or "default", reason=str(e))
            raise HTTPException(status_code=400, detail=f"undecodable frame: {e}") from e
        except Exception as e:  # pragma: no cover - analyze() is written not to raise
            log_event("analyze_failed", level=logging.ERROR,
                      sessionId=request.sessionId or "default", error=type(e).__name__)
            raise HTTPException(status_code=500, detail=f"vision failure: {type(e).__name__}") from e

        stats.frames_processed += 1
        stats.total_latency_ms += float(body.get("inference_latency_ms") or 0.0)
        if not body.get("face_detected"):
            stats.frames_without_face += 1
        log_event("frame_analyzed", level=logging.DEBUG,
                  sessionId=request.sessionId or "default",
                  mode=body.get("mode"),
                  face_detected=body.get("face_detected"),
                  latency_ms=body.get("inference_latency_ms"))
        return body

    return app


DEFAULT_PORT = 8000
DEFAULT_HOST = "0.0.0.0"


class ConfigError(RuntimeError):
    """A setting this service owns is present but unusable. See :func:`resolve_port`.

    Defined locally rather than imported from ``fatigue_xai.config`` so the vision
    image does not need the narrative package merely to parse its own port. The
    *policy* is shared even though the twelve lines are not; both files name the
    other so the pair stays in step.
    """


def resolve_port() -> int:
    """Bind port from ``VISION_PORT``, falling back to a *usable* generic ``PORT``.

    ``VISION_PORT`` is the documented setting and always wins. A generic ``PORT`` is
    honoured for container platforms that inject it, but only when it is a real port
    number: ``PORT=0`` (which some harnesses export, and which uvicorn reads as "bind
    any free port") would otherwise move a service the gateway must reach at a fixed
    address onto a random port — indistinguishable from the service being down.

    The two names are treated differently on purpose, and this is the policy the
    narrative service follows as well (see ``fatigue_xai/config.py``, which owns
    the canonical implementation for that service):

    * ``VISION_PORT`` is **ours**. A value that is present but unusable is a
      configuration error and the process refuses to start (:class:`ConfigError`),
      rather than serving from an address nobody is looking for.
    * ``PORT`` is **the host's**. It is injected by container platforms and CI
      harnesses that do not know about this project, so an unusable value is
      ignored and we fall back to the documented default. Failing to start because
      an unrelated tool exported ``PORT=0`` would be worse than binding our default.

    Only bare decimal digits are accepted; ``int()`` would also accept ``8_100``
    (= 8100) and a leading ``+``, which are typos rather than intent.
    """
    own = os.getenv("VISION_PORT")
    if own is not None and own.strip():
        value = _parse_own_port("VISION_PORT", own.strip())
        return value

    generic = os.getenv("PORT")
    if generic is not None and generic.strip():
        text = generic.strip()
        if _PORT_DIGITS.match(text):
            value = int(text, 10)
            if 1 <= value <= 65535:
                return value
        # Not usable, but not ours to complain about.
        log.warning("vision: ignoring unusable PORT=%r; using the default %d",
                    generic, DEFAULT_PORT)
    return DEFAULT_PORT


#: Bare decimal digits only — see :func:`resolve_port` for why.
_PORT_DIGITS = re.compile(r"^[0-9]+$")


def _parse_own_port(name: str, text: str) -> int:
    """Validate a setting this service owns; raise :class:`ConfigError` if unusable."""
    if not _PORT_DIGITS.match(text):
        raise ConfigError(
            f"{name}={text!r} is not a port number. Set {name} to a whole number "
            f"between 1 and 65535, or unset it to use the default ({DEFAULT_PORT})."
        )
    value = int(text, 10)
    if value == 0:
        raise ConfigError(
            f"{name}=0 asks the OS to allocate a random port. The gateway connects "
            f"to this service at a configured URL (VISION_SERVICE_URL) and cannot "
            f"discover a random one. Unset {name} to use the default ({DEFAULT_PORT})."
        )
    if value > 65535:
        raise ConfigError(
            f"{name}={value} is outside the usable port range 1-65535. "
            f"Unset it to use the default ({DEFAULT_PORT})."
        )
    return value


def resolve_host() -> str:
    return os.getenv("VISION_HOST") or os.getenv("HOST") or DEFAULT_HOST


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    # Validate before binding. A bad VISION_PORT must be one readable line and
    # exit 2, not a traceback, and never a silent bind somewhere else.
    try:
        host, port = resolve_host(), resolve_port()
    except ConfigError as e:
        print(f"vision_service: configuration error: {e}", file=sys.stderr)
        raise SystemExit(2) from None

    log_event("serving", host=host, port=port, pid=os.getpid())
    uvicorn.run("vision_service.app:app", host=host, port=port,
                log_level=os.getenv("LOG_LEVEL", "info").lower())
