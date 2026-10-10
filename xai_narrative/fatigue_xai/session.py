"""Per-operator session: frame in -> assessment/narrative events out.

Frame ingestion never waits for the LLM.  Narration runs as a background task;
a newer, more severe assessment pre-empts an in-flight narrative, a less severe
one is coalesced (latest wins) and runs when the current one finishes.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Union

from .narrative import NarrationPolicy, NarrativeService
from .rules import FacsRuleEngine, RuleConfig
from .schemas import LEVELS, Assessment, FrameTelemetry

Emit = Callable[[Dict[str, Any]], Union[Awaitable[None], None]]


def _rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 99 if level == "UNKNOWN" else -1


class OperatorSession:
    def __init__(self, session_id: str, narrator: NarrativeService, emit: Optional[Emit] = None,
                 cfg: Optional[RuleConfig] = None):
        self.session_id = session_id
        self.engine = FacsRuleEngine(session_id, cfg)
        self.policy = NarrationPolicy()
        self.narrator = narrator
        self.emit = emit
        self.narratives: Deque[Dict[str, Any]] = deque(maxlen=200)
        self._task: Optional[asyncio.Task] = None
        self._running_rank = -1
        self._pending: Optional[Assessment] = None

    @property
    def latest(self) -> Optional[Assessment]:
        return self.engine.history[-1] if self.engine.history else None

    async def _send(self, ev: Dict[str, Any]) -> None:
        if self.emit is None:
            return
        res = self.emit(ev)
        if asyncio.iscoroutine(res):
            await res

    async def on_frame(self, frame: FrameTelemetry) -> Optional[Assessment]:
        a = self.engine.ingest(frame)
        if a is None:
            return None
        await self._send({"type": "assessment", "session_id": self.session_id,
                          "assessment": a.model_dump()})
        if self.policy.should_narrate(a):
            self._schedule(a)
        return a

    def _schedule(self, a: Assessment) -> None:
        r = _rank(a.risk_level)
        if self._task and not self._task.done():
            if r > self._running_rank:
                self._task.cancel()
                self._start(a)
            else:
                self._pending = a
            return
        self._start(a)

    def _start(self, a: Assessment) -> None:
        self._running_rank = _rank(a.risk_level)
        self._task = asyncio.create_task(self._run(a))

    async def _run(self, a: Assessment) -> None:
        try:
            async for ev in self.narrator.stream(a):
                if ev["type"] == "narrative_final":
                    self.narratives.append(ev)
                await self._send(ev)
        except asyncio.CancelledError:
            await self._send({"type": "narrative_cancelled", "session_id": self.session_id})
            raise
        finally:
            nxt, self._pending = self._pending, None
            if nxt is not None and asyncio.current_task() is self._task:
                self._start(nxt)

    async def drain(self) -> None:
        """Wait for in-flight narration (tests / graceful shutdown)."""
        while self._task and not self._task.done():
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class SessionRegistry:
    def __init__(self, narrator: NarrativeService, cfg: Optional[RuleConfig] = None):
        self.narrator, self.cfg = narrator, cfg
        self.sessions: Dict[str, OperatorSession] = {}

    def get(self, session_id: str, emit: Optional[Emit] = None) -> OperatorSession:
        s = self.sessions.get(session_id)
        if s is None:
            s = self.sessions[session_id] = OperatorSession(session_id, self.narrator, emit, self.cfg)
        elif emit is not None:
            s.emit = emit
        return s

    def drop(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
