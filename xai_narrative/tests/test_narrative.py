import asyncio, json
import httpx, pytest
from fatigue_xai.llm import OllamaClient, OpenAICompatClient, ScriptedLLM
from fatigue_xai.narrative import NarrationPolicy, NarrativeConfig, NarrativeService, compose, template_body
from fatigue_xai.prompt import build_messages, facts_view
from fatigue_xai.rules import FacsRuleEngine
from fatigue_xai.guard import validate
from fatigue_xai.simulator import generate

def assessment_at(t_end, scenario="drift_to_microsleep", level=None):
    frames, *_ = generate(scenario)
    e = FacsRuleEngine("sim")
    last = None
    for f in frames:
        if f.ts > t_end:
            break
        last = e.ingest(f) or last
    return last


def good_text(a):
    """A faithful LLM answer built from the facts view (what a good model should produce)."""
    v = facts_view(a)
    return (f"Risk is {v['risk_level']} and {v['trend'] if v['trend'] != 'unknown' else 'being tracked'}, with the fatigue probability at "
            f"{v['fatigue_probability_percent']}%. " + "; ".join(v["findings"]) + ".")


def run(coro):
    return asyncio.run(coro)


def test_llm_good_answer_used():
    a = assessment_at(184)           # HIGH with findings
    assert a.risk_level == "HIGH"
    llm = ScriptedLLM([good_text(a)])
    ev = run(NarrativeService(llm).narrate(a))
    assert ev["source"] == "llm" and ev["guard"]["passed"], ev["guard"]
    assert ev["text"].startswith(f"[{a.ts_str}] HIGH RISK - ")
    assert ev["text"].endswith("Recommended action: " + a.recommended_action)


def test_chunks_are_provisional_then_final():
    a = assessment_at(184)
    evs = []
    async def go():
        async for e in NarrativeService(ScriptedLLM([good_text(a)])).stream(a):
            evs.append(e)
    run(go())
    types = [e["type"] for e in evs]
    assert types[0] == "narrative_start" and types[-1] == "narrative_final" and "narrative_chunk" in types
    assert all(e.get("provisional") for e in evs if e["type"] == "narrative_chunk")


def test_hallucination_then_retry_succeeds():
    a = assessment_at(184)
    bad = "Risk is HIGH with the fatigue probability at 99%. The driver should pull over."
    llm = ScriptedLLM([bad, good_text(a)])
    ev = run(NarrativeService(llm).narrate(a))
    assert ev["source"] == "llm" and ev["guard"]["attempts"] == 2
    # the retry prompt must tell the model why it was rejected
    assert "rejected" in llm.calls[1][-1]["content"]


def test_persistent_hallucination_falls_back_to_template():
    a = assessment_at(184)
    llm = ScriptedLLM(["Risk is HIGH at 99% and the driver has sleep apnea."])
    ev = run(NarrativeService(llm).narrate(a))
    assert ev["source"] == "template_fallback" and not ev["guard"]["passed"] and ev["guard"]["violations"]
    # fallback text must itself satisfy the guard's factual checks
    assert "99" not in ev["text"] and "apnea" not in ev["text"]


def test_llm_down_falls_back():
    a = assessment_at(184)
    ev = run(NarrativeService(ScriptedLLM([""], fail=True)).narrate(a))
    assert ev["source"] == "template_fallback" and "llm unavailable" in ev["guard"]["violations"][0]


def test_llm_timeout_falls_back():
    a = assessment_at(184)
    cfg = NarrativeConfig(first_token_timeout_s=0.05, total_timeout_s=0.5)
    ev = run(NarrativeService(ScriptedLLM([good_text(a)], delay_s=0.5), cfg).narrate(a))
    assert ev["source"] == "template_fallback"


def test_critical_never_waits_for_llm():
    a = assessment_at(236)
    assert a.risk_level == "CRITICAL"
    llm = ScriptedLLM([good_text(a)])
    ev = run(NarrativeService(llm).narrate(a))
    assert ev["source"] == "template" and llm.calls == [] and ev["latency_ms"] < 50


def test_no_llm_configured_uses_template():
    a = assessment_at(184)
    ev = run(NarrativeService(None).narrate(a))
    assert ev["source"] == "template"


def test_template_passes_numeric_guard():
    """The deterministic fallback must be consistent with the same facts the guard enforces."""
    for t in (150, 184, 212):
        a = assessment_at(t)
        body = template_body(a)
        r = validate(body, facts_view(a), max_words=200)
        assert not [v for v in r.violations if "not in facts" in v or "AU" in v], (t, r.violations)


def test_unknown_level_template():
    a = assessment_at(70, "occlusion")
    assert a.risk_level == "UNKNOWN"
    txt = compose(a, template_body(a))
    assert "RISK UNKNOWN" in txt and "camera" in txt.lower()


def test_prompt_contains_only_prerendered_facts():
    a = assessment_at(184)
    msgs = build_messages(a)
    assert msgs[0]["role"] == "system" and "ONLY information" in msgs[0]["content"]
    last = msgs[-1]["content"]
    assert "FACTS" in last and "class_probs" not in last and "ear" not in json.dumps(facts_view(a)).lower().split('"')[1::2][:1]


def test_policy_escalation_and_debounce():
    p = NarrationPolicy()
    mk = lambda t, lvl, ids=(): type("A", (), dict(calibrating=False, risk_level=lvl, ts=t, early_drift=False,
                                                    findings=[], finding_ids=lambda s=None, ids=ids: list(ids)))()
    assert not p.should_narrate(mk(0, "LOW"))
    assert p.should_narrate(mk(10, "MODERATE"))
    assert not p.should_narrate(mk(14, "MODERATE"))
    assert p.should_narrate(mk(16, "HIGH"))                  # escalation: immediate
    assert not p.should_narrate(mk(20, "MODERATE"))          # de-escalation debounced
    assert p.should_narrate(mk(32, "MODERATE"))              # >= 15 s since last
    assert p.should_narrate(mk(70, "MODERATE"))              # repeat after 30 s


# ---- real HTTP client parsing against a fake server -------------------------
def _ollama_transport(text, error=None):
    def handler(req: httpx.Request):
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        body = json.loads(req.content)
        assert body["stream"] is True and body["options"]["temperature"] == 0 and body["options"]["seed"] == 42
        if error:
            return httpx.Response(200, content=json.dumps({"error": error}) + "\n")
        words = text.split(" ")
        lines = [json.dumps({"message": {"role": "assistant", "content": w + " "}, "done": False}) for w in words]
        lines.append(json.dumps({"done": True}))
        return httpx.Response(200, content="\n".join(lines) + "\n")
    return httpx.MockTransport(handler)


def test_ollama_client_streams_and_is_deterministic_config():
    a = assessment_at(184)
    llm = OllamaClient(transport=_ollama_transport(good_text(a)))
    ev = run(NarrativeService(llm).narrate(a))
    assert ev["source"] == "llm", ev["guard"]
    assert run(llm.is_up())


def test_ollama_error_line_falls_back():
    a = assessment_at(184)
    llm = OllamaClient(transport=_ollama_transport("", error="model not found"))
    assert run(NarrativeService(llm).narrate(a))["source"] == "template_fallback"


def test_openai_compat_client_streams():
    a = assessment_at(184)
    text = good_text(a)
    def handler(req):
        chunks = [f'data: {json.dumps({"choices": [{"delta": {"content": w + " "}}]})}' for w in text.split(" ")]
        return httpx.Response(200, content="\n\n".join(chunks + ["data: [DONE]"]) + "\n\n")
    llm = OpenAICompatClient(transport=httpx.MockTransport(handler))
    assert run(NarrativeService(llm).narrate(a))["source"] == "llm"
