"""Talk to the narrative MCP server over stdio, exactly as an MCP host would.

    python examples/mcp_client_demo.py            # uses NARRATIVE_LLM from env (default: ollama)
    NARRATIVE_LLM=none python examples/mcp_client_demo.py   # template-only, no LLM needed
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fatigue_xai.simulator import generate  # noqa: E402


def _payload(result):
    """Tool result -> python object (structured content if present, else JSON text)."""
    sc = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if sc:
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "fatigue_xai.mcp_server"],
                                   env={**os.environ})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            frames, _, _ = generate("drift_to_microsleep")
            batch = [json.loads(f.model_dump_json()) for f in frames if 120 <= f.ts <= 185]
            # replay the baseline period first so the per-user baseline exists
            warm = [json.loads(f.model_dump_json()) for f in frames if f.ts < 120]
            await session.call_tool("ingest_telemetry_batch", {"frames": warm})
            await session.call_tool("ingest_telemetry_batch", {"frames": batch})

            a = _payload(await session.call_tool("get_operator_assessment", {"session_id": "sim"}))
            print("risk:", a["risk_level"], "| findings:", [f["id"] for f in a["findings"]])
            n = _payload(await session.call_tool("generate_risk_narrative", {"session_id": "sim"}))
            print("\nNARRATIVE (%s):\n%s" % (n["source"], n["text"]))
            r = _payload(await session.call_tool("explain_rule", {"rule_id": "FIXED_STARE"}))
            print("\nexplain_rule:", r["rule"])


if __name__ == "__main__":
    asyncio.run(main())
