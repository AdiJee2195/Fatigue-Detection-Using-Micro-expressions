import asyncio, json
import pytest
from fastapi.testclient import TestClient
from fatigue_xai.api import build_registry, create_app
from fatigue_xai.llm import ScriptedLLM
from fatigue_xai.mcp_server import create_server
from fatigue_xai.narrative import NarrativeConfig, NarrativeService
from fatigue_xai.prompt import facts_view
from fatigue_xai.session import OperatorSession
from fatigue_xai.simulator import generate


def faithful(a):
    v = facts_view(a)
    return (f"Risk is {v['risk_level']} and {v['trend'] if v['trend'] != 'unknown' else 'being tracked'}, with the fatigue "
            f"probability at {v['fatigue_probability_percent']}%. " + "; ".join(v["findings"]) + ".")


class EchoLLM(ScriptedLLM):
    """Builds a faithful answer from the FACTS in the prompt it receives."""
    async def stream(self, messages):
        facts = json.loads(messages[-1]["content"].split("FACTS:\n", 1)[1].split("\n\n")[0])
        text = (f"Risk is {facts['risk_level']} and {facts['trend'] if facts['trend'] != 'unknown' else 'being tracked'}, "
                f"with the fatigue probability at {facts['fatigue_probability_percent']}%. " + "; ".join(facts["findings"]) + ".")
        self.answers = [text]
        async for c in super().stream(messages):
            yield c


def test_ws_roundtrip_and_event_order():
    reg = build_registry(llm=EchoLLM([""]), use_env_llm=False)
    frames, *_ = generate("drift_to_microsleep")
    with TestClient(create_app(reg)) as client:
        with client.websocket_connect("/ws/sim") as ws:
            for f in frames:
                if 100 <= f.ts <= 190:
                    ws.send_text(f.model_dump_json())
            ws.send_text("not json")
            # drain: collect until we see the error reply and at least one final narrative
            evs, finals = [], 0
            for _ in range(2000):
                ev = ws.receive_json()
                evs.append(ev)
                finals += ev["type"] == "narrative_final"
                if ev["type"] == "error" and finals:
                    break
                if ev["type"] == "error":
                    # wait for narration tasks to complete
                    pass
            types = {e["type"] for e in evs}
            assert {"assessment", "narrative_start", "narrative_final", "error"} <= types
            fin = [e for e in evs if e["type"] == "narrative_final"][0]
            assert fin["source"] in ("llm", "template") and fin["text"].startswith("[T+")


def test_preview_endpoint_and_validation():
    reg = build_registry(llm=EchoLLM([""]), use_env_llm=False)
    frames, *_ = generate("drift_to_microsleep")
    body = {"frames": [json.loads(f.model_dump_json()) for f in frames if f.ts <= 184]}
    with TestClient(create_app(reg)) as c:
        r = c.post("/v1/narrative/preview", json=body)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["assessment"]["risk_level"] in ("HIGH", "MODERATE", "CRITICAL")
        assert j["narrative"]["source"] == "llm" and j["narrative"]["guard"]["passed"]
        assert c.post("/v1/narrative/preview", json={"frames": []}).status_code == 422
        bad = {"frames": [{"ts": 1, "class_probs": {"neutral": 0.2}}]}
        assert c.post("/v1/narrative/preview", json=bad).status_code == 422
        assert c.get("/health").json()["status"] == "ok"
        assert c.get("/v1/sessions/nope/assessment").status_code == 404


def test_ingest_never_blocked_by_slow_llm():
    """A 2 s LLM must not stall frame processing (async decoupling)."""
    async def go():
        slow = ScriptedLLM(["x"], delay_s=2.0)
        sess = OperatorSession("s", NarrativeService(slow, NarrativeConfig(first_token_timeout_s=5, total_timeout_s=10)))
        frames, *_ = generate("drift_to_microsleep")
        import time
        t0 = time.perf_counter()
        n = 0
        for f in frames:
            if 100 <= f.ts <= 185:
                await sess.on_frame(f)
                n += 1
        dt = time.perf_counter() - t0
        await sess.close()
        return n, dt
    n, dt = asyncio.run(go())
    assert n > 1800 and dt < 1.5, (n, dt)   # ~2000 frames ingested while the LLM was 'busy'


def test_escalation_preempts_running_narrative():
    async def go():
        evs = []
        llm = ScriptedLLM(["Risk is MODERATE with the fatigue probability at 1%."], delay_s=0.3)
        cfg = NarrativeConfig(first_token_timeout_s=5, total_timeout_s=10)
        sess = OperatorSession("s", NarrativeService(llm, cfg), emit=lambda e: evs.append(e))
        frames, *_ = generate("drift_to_microsleep")
        for f in frames:
            if 100 <= f.ts <= 240:
                await sess.on_frame(f)
                if f.frame_id % 12 == 0:
                    await asyncio.sleep(0.003)
        await sess.drain()
        return evs
    evs = asyncio.run(go())
    assert any(e["type"] == "narrative_cancelled" for e in evs)
    finals = [e for e in evs if e["type"] == "narrative_final"]
    assert finals and finals[-1]["risk_level"] == "CRITICAL"


def test_mcp_tools_and_resources():
    reg = build_registry(llm=EchoLLM([""]), use_env_llm=False)
    server = create_server(reg)
    frames, *_ = generate("drift_to_microsleep")

    async def go():
        batch = [json.loads(f.model_dump_json()) for f in frames if f.ts <= 184]
        r = await server.call_tool("ingest_telemetry_batch", {"frames": batch})
        a = await server.call_tool("get_operator_assessment", {"session_id": "sim"})
        n = await server.call_tool("generate_risk_narrative", {"session_id": "sim"})
        e = await server.call_tool("explain_rule", {"rule_id": "brow_strain"})
        tl = await server.call_tool("get_risk_timeline", {"session_id": "sim", "last_n": 5})
        res = await server.read_resource("facs://rules/catalogue")
        return r, a, n, e, tl, res
    r, a, n, e, tl, res = asyncio.run(go())

    def payload(x):
        """Normalise call_tool results across MCP SDK 1.x (content list / tuple) and 2.x (result object)."""
        sc = getattr(x, "structured_content", None) or getattr(x, "structuredContent", None)
        if sc:
            return sc.get("result", sc)
        if hasattr(x, "content"):
            x = x.content
        if isinstance(x, tuple):
            return payload(x[1]) if len(x) > 1 and x[1] else payload(x[0])
        if isinstance(x, dict):
            return x.get("result", x)
        if isinstance(x, list):
            items = [json.loads(c.text) if hasattr(c, "text") else c for c in x]
            return items[0] if len(items) == 1 and isinstance(items[0], dict) else items
        return x
    assert payload(a)["session_id"] == "sim"
    assert payload(n)["source"] == "llm"
    assert "AU4" in json.dumps(payload(e))
    assert len(payload(tl)) == 5
    assert "FIXED_STARE" in str(res)


def test_mcp_rejects_bad_frame():
    server = create_server(build_registry(llm=None, use_env_llm=False))
    async def go():
        return await server.call_tool("ingest_telemetry", {"frame": {"ts": 1, "class_probs": {"neutral": 0.1}}})
    with pytest.raises(Exception):
        asyncio.run(go())
