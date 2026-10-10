"""FastAPI service: the integration surface for the Java Spring Boot gateway.

WebSocket  /ws/{session_id}
    in : one JSON FrameTelemetry per message (session_id taken from the URL)
    out: {"type": "assessment" | "narrative_start" | "narrative_chunk" |
                  "narrative_retry" | "narrative_final" | "narrative_cancelled" | "error", ...}
REST
    POST /v1/narrative/preview      stateless: list of frames -> assessment + narrative (Postman friendly)
    GET  /v1/sessions/{id}/assessment
    GET  /v1/sessions/{id}/narratives?limit=20
    GET  /health

UI contract: ``narrative_chunk`` text is PROVISIONAL.  Replace whatever has been
streamed with ``narrative_final.text`` (validated, or the deterministic fallback).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from .config import ConfigError
from .errors import REASON_INVALID_TELEMETRY_FRAME
from .events import NarrativeQueue
from .llm import OllamaClient, llm_from_env
from .narrative import NarrativeConfig, NarrativeService
from .rules import FacsRuleEngine, RuleConfig
from .schemas import FrameTelemetry
from .session import SessionRegistry

log = logging.getLogger("fatigue_xai.api")


def build_registry(llm=None, use_env_llm: bool = True, rule_cfg: Optional[RuleConfig] = None,
                   narrative_cfg: Optional[NarrativeConfig] = None) -> SessionRegistry:
    if llm is None and use_env_llm:
        llm = llm_from_env()
    cfg_path = os.getenv("RULES_JSON")
    if rule_cfg is None and cfg_path:
        rule_cfg = RuleConfig.from_json(cfg_path)
    return SessionRegistry(NarrativeService(llm, narrative_cfg), rule_cfg)


class PreviewRequest(BaseModel):
    frames: List[FrameTelemetry]


def create_app(registry: Optional[SessionRegistry] = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.registry = registry or build_registry()
        llm = app.state.registry.narrator.llm
        if isinstance(llm, OllamaClient) and not await llm.is_up():
            log.warning("Ollama not reachable at %s - narratives will use the deterministic template fallback.",
                        llm.base_url)
        yield
        for s in list(app.state.registry.sessions.values()):
            await s.close()

    app = FastAPI(title="Fatigue Narrative XAI Service", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        """Liveness plus the narrative provider's *observed* state.

        Only Ollama is actively probed: it is local and free. An external provider
        (Groq) is never probed here — an orchestrator polling every 10 s would spend
        the operator's quota to answer a question the last real call already
        answered. ``provider`` therefore reports what was observed.
        """
        reg: SessionRegistry = app.state.registry
        llm = reg.narrator.llm
        up = await llm.is_up() if isinstance(llm, OllamaClient) else None
        provider_status = reg.narrator.provider_status()
        return {
            "status": "ok",
            "llm": reg.narrator.provider_name() or "none",
            "llm_reachable": up,
            "provider": provider_status.as_dict() if provider_status else {
                "enabled": False, "model": None,
                "reason": "no provider configured (NARRATIVE_LLM=none)",
                "calls": 0, "failures": 0, "last_error_kind": None,
                "last_error_at": None, "last_error_message": None, "last_ok_at": None,
            },
            "sessions": len(reg.sessions),
        }

    @app.websocket("/ws/{session_id}")
    async def ws_endpoint(ws: WebSocket, session_id: str) -> None:
        await ws.accept()
        reg: SessionRegistry = app.state.registry
        # Buffer policy lives in events.NarrativeQueue: replaceable chunks are
        # coalesced rather than dropped, and an authoritative narrative_final is
        # never discarded in favour of a provisional one. The previous inline
        # asyncio.Queue dropped whatever was oldest, which could silently lose the
        # only text the operator is supposed to trust.
        queue = NarrativeQueue()

        async def emit(ev: Dict[str, Any]) -> None:
            queue.put_nowait(ev)

        async def sender() -> None:
            while True:
                event = await queue.get()
                if event is None:
                    return
                await ws.send_json(event)

        sess = reg.get(session_id, emit)
        send_task = asyncio.create_task(sender())
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    data = json.loads(raw)
                    data["session_id"] = session_id
                    frame = FrameTelemetry(**data)
                except (json.JSONDecodeError, ValidationError, TypeError) as e:
                    # `reason` is the canonical machine-readable code the gateway and
                    # dashboard switch on; `message`/`detail` are for a human reading
                    # the logs. The gateway relays whichever of the two it finds, so
                    # both are sent — the code must not be prose, or every consumer
                    # ends up string-matching on English.
                    await emit({
                        "type": "error",
                        "reason": REASON_INVALID_TELEMETRY_FRAME,
                        "message": "invalid telemetry frame",
                        "detail": str(e)[:300],
                    })
                    continue
                await sess.on_frame(frame)
        except WebSocketDisconnect:
            pass
        finally:
            # Close the buffer first so the sender wakes and returns instead of
            # being cancelled mid-await, then stop the task.
            queue.close()
            send_task.cancel()
            await sess.close()
            reg.drop(session_id)

    @app.post("/v1/narrative/preview")
    async def preview(req: PreviewRequest) -> Dict[str, Any]:
        reg: SessionRegistry = app.state.registry
        if not req.frames:
            raise HTTPException(422, "frames must not be empty")
        cfg = replace(reg.cfg or RuleConfig(), assess_every_s=0.0)
        engine = FacsRuleEngine(req.frames[0].session_id, cfg)
        a = None
        for f in req.frames:
            a = engine.ingest(f) or a
        if a is None:
            raise HTTPException(422, "not enough frames to produce an assessment")
        final = await reg.narrator.narrate(a)
        return {"assessment": a.model_dump(), "narrative": final}

    @app.get("/v1/sessions/{session_id}/assessment")
    async def get_assessment(session_id: str) -> Dict[str, Any]:
        s = app.state.registry.sessions.get(session_id)
        if not s or not s.latest:
            raise HTTPException(404, "no assessment yet for this session")
        return s.latest.model_dump()

    @app.get("/v1/sessions/{session_id}/narratives")
    async def get_narratives(session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        s = app.state.registry.sessions.get(session_id)
        if not s:
            raise HTTPException(404, "unknown session")
        return list(s.narratives)[-max(1, min(limit, 200)):]

    return app


app = create_app()

if __name__ == "__main__":  # pragma: no cover
    import sys

    import uvicorn

    from .config import resolve_host, resolve_port

    # Validate before importing anything heavy. A bad port must produce one
    # readable line and exit 2 — "your configuration is wrong" — rather than a
    # ValueError traceback from inside uvicorn, or a silent bind to a random port
    # that the gateway can never find. See fatigue_xai/config.py for both cases.
    try:
        host, port = resolve_host(), resolve_port()
    except ConfigError as e:
        print(f"fatigue_xai: configuration error: {e}", file=sys.stderr)
        raise SystemExit(2) from None

    log.info("serving narrative XAI on %s:%s", host, port)
    uvicorn.run("fatigue_xai.api:app", host=host, port=port)
