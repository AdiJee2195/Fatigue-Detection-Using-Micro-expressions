"""MCP server for the narrative layer (RO4: "MCP-mediated Explainable AI layer").

Exposes the deterministic FACS engine and the guarded narrator as MCP tools,
resources and a prompt, so any MCP host (a supervisor chat UI, Claude Desktop,
an agent) can ask "why is operator X at HIGH risk?" and get facts that were
produced by rules, not by the model.

Run:
    python -m fatigue_xai.mcp_server                       # stdio
    python -m fatigue_xai.mcp_server --transport http      # streamable HTTP on :8200

Works with the MCP Python SDK 1.x (FastMCP) and 2.x (MCPServer).
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

try:                                    # MCP SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                     # MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .api import build_registry
from .prompt import SYSTEM_PROMPT, build_messages, facts_view
from .rules import rule_catalogue
from .schemas import FrameTelemetry
from .session import SessionRegistry

INSTRUCTIONS = (
    "Driver-fatigue narrative service. Feed per-frame telemetry with ingest_telemetry, then use "
    "get_operator_assessment for the deterministic facts and generate_risk_narrative for a validated, "
    "timestamped Operator Risk Assessment. Never state numbers that are not in an assessment."
)


def create_server(registry: Optional[SessionRegistry] = None):
    reg = registry or build_registry()
    mcp = _Server("fatigue-narrative-xai", instructions=INSTRUCTIONS)

    def _frame(d: Dict[str, Any]) -> FrameTelemetry:
        try:
            return FrameTelemetry(**d)
        except ValidationError as e:
            raise ValueError(f"invalid telemetry frame: {e.errors()[0]['msg']} at {e.errors()[0]['loc']}") from None

    @mcp.tool()
    async def ingest_telemetry(frame: Dict[str, Any]) -> Dict[str, Any]:
        """Ingest ONE frame of CV telemetry for an operator session.

        Required: ts (seconds), class_probs ({'microsleep','neutral','yawning'} summing to 1).
        Optional: session_id, ear, au ({'AU4': 0-5, ...}), rppg_bpm, face_detected, illumination.
        Returns the new deterministic assessment when one is due (every ~2 s), otherwise accepted=true only.
        """
        f = _frame(frame)
        a = await reg.get(f.session_id).on_frame(f)
        return {"accepted": True, "assessment": a.model_dump() if a else None}

    @mcp.tool()
    async def ingest_telemetry_batch(frames: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Ingest many frames (oldest first). Returns the latest assessment produced, if any."""
        last = None
        for d in frames:
            f = _frame(d)
            last = (await reg.get(f.session_id).on_frame(f)) or last
        return {"accepted": len(frames), "assessment": last.model_dump() if last else None}

    @mcp.tool()
    def get_operator_assessment(session_id: str = "default") -> Dict[str, Any]:
        """Latest deterministic assessment: risk level, trend, findings (with FACS codes and evidence), data quality."""
        s = reg.sessions.get(session_id)
        if not s or not s.latest:
            raise ValueError(f"no assessment yet for session '{session_id}'")
        return s.latest.model_dump()

    @mcp.tool()
    async def generate_risk_narrative(session_id: str = "default") -> Dict[str, Any]:
        """Produce a timestamped Operator Risk Assessment for the latest assessment.

        Uses the local LLM under strict FACS prompting, validates the text against the facts,
        and falls back to a deterministic template if validation fails. `source` tells which.
        """
        s = reg.sessions.get(session_id)
        if not s or not s.latest:
            raise ValueError(f"no assessment yet for session '{session_id}'")
        final = await reg.narrator.narrate(s.latest)
        s.narratives.append(final)
        return final

    @mcp.tool()
    def get_narrative_history(session_id: str = "default", limit: int = 10) -> List[Dict[str, Any]]:
        """Most recent narratives for the session (oldest first)."""
        s = reg.sessions.get(session_id)
        if not s:
            raise ValueError(f"unknown session '{session_id}'")
        return list(s.narratives)[-max(1, min(limit, 100)):]

    @mcp.tool()
    def get_risk_timeline(session_id: str = "default", last_n: int = 30) -> List[Dict[str, Any]]:
        """Compact risk timeline (time, level, smoothed probability, trend, finding ids) for trajectory questions."""
        s = reg.sessions.get(session_id)
        if not s:
            raise ValueError(f"unknown session '{session_id}'")
        hist = list(s.engine.history)[-max(1, min(last_n, 500)):]
        return [{"ts": a.ts_str, "risk_level": a.risk_level, "p_fatigue": a.p_fatigue_smoothed,
                 "trend": a.trend, "findings": a.finding_ids()} for a in hist]

    @mcp.tool()
    def explain_rule(rule_id: str) -> Dict[str, Any]:
        """Explain how a finding id (e.g. FIXED_STARE, BROW_STRAIN) is detected and which FACS codes it uses."""
        for r in rule_catalogue(reg.cfg):
            if r["id"].lower() == rule_id.lower():
                return r
        raise ValueError(f"unknown rule '{rule_id}'. Known: {[r['id'] for r in rule_catalogue(reg.cfg)]}")

    @mcp.resource("facs://rules/catalogue")
    def rules_resource() -> str:
        """All deterministic FACS rules and their current thresholds."""
        return json.dumps(rule_catalogue(reg.cfg), indent=2)

    @mcp.resource("facs://prompt/system")
    def system_prompt_resource() -> str:
        """The strict system prompt the narrator uses."""
        return SYSTEM_PROMPT

    @mcp.resource("session://{session_id}/assessment")
    def session_assessment(session_id: str) -> str:
        """Latest assessment of a session as JSON."""
        s = reg.sessions.get(session_id)
        return json.dumps(s.latest.model_dump() if s and s.latest else {"error": "no assessment yet"}, indent=2)

    @mcp.prompt()
    def operator_risk_assessment(session_id: str = "default") -> str:
        """Ready-to-use prompt for an MCP host LLM to write the narrative itself, with the same strict rules."""
        s = reg.sessions.get(session_id)
        if not s or not s.latest:
            return "No assessment is available yet for this session."
        return SYSTEM_PROMPT + "\n\nFACTS:\n" + json.dumps(facts_view(s.latest))

    mcp.registry = reg  # type: ignore[attr-defined]  (handy for tests / embedding)
    return mcp


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8200)
    args = ap.parse_args()
    server = create_server()
    if args.transport == "stdio":
        server.run()
    else:
        import uvicorn
        uvicorn.run(server.streamable_http_app(), host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
