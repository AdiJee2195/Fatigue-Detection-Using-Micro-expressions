"""Groq provider: configuration, the full error matrix, and narrative integration.

Everything here runs against an in-process ``httpx.MockTransport``. No network, no
account, no paid call. That is deliberate: unit tests must be runnable by any
reviewer, and a test that needs a live key is not a test.

What a mock *cannot* prove is that the real Groq endpoint is reachable and that
this request shape is accepted by it. That is a separate, explicitly-labelled
live smoke test (``scripts/groq_smoke.py``); see the README.
"""
import asyncio
import json

import httpx
import pytest

from fatigue_xai.config import ConfigError
from fatigue_xai.llm import (
    GROQ_DEFAULT_BASE_URL,
    GROQ_DEFAULT_MAX_TOKENS,
    GROQ_DEFAULT_TIMEOUT_S,
    ErrorKind,
    GroqClient,
    LLMError,
    classify,
    kind_from_status,
    llm_from_env,
    redact,
)
from fatigue_xai.narrative import (
    LLM_DISABLED,
    LLM_GUARD_REJECTED,
    LLM_OK,
    LLM_SKIPPED,
    LLM_UNAVAILABLE,
    SOURCE_FALLBACK,
    SOURCE_LLM,
    SOURCE_TEMPLATE,
    NarrativeConfig,
    NarrativeService,
)
from fatigue_xai.rules import FacsRuleEngine
from fatigue_xai.simulator import generate

KEY = "gsk_test_key_do_not_log_me_1234567890"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sse(*chunks, done=True):
    """Build an OpenAI-compatible SSE body from text chunks."""
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}))
    if done:
        lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def client(handler, **kw):
    """A GroqClient wired to a MockTransport, configured by default."""
    kw.setdefault("api_key", KEY)
    kw.setdefault("model", "test-model")
    kw.setdefault("transport", httpx.MockTransport(handler))
    return GroqClient(**kw)


def answering(text="Risk is HIGH.", status=200):
    def handler(request):
        return httpx.Response(status, content=sse(text))
    return handler


def erroring(status, message="upstream error"):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": message}})
    return handler


def assessment_at(t_end, scenario="drift_to_microsleep"):
    frames, *_ = generate(scenario)
    engine = FacsRuleEngine("sim")
    last = None
    for f in frames:
        if f.ts > t_end:
            break
        last = engine.ingest(f) or last
    return last


def facts_of(a):
    from fatigue_xai.prompt import facts_view
    return facts_view(a)


def grounded_answer(a):
    """A faithful answer built from the facts view — what a good model should emit.

    Built rather than hand-written on purpose: hard-coding the trend word ('rising')
    makes the test depend on a scenario detail, and the guard rightly rejects a
    trend that contradicts the assessment.
    """
    v = facts_of(a)
    trend = v["trend"] if v["trend"] != "unknown" else "being tracked"
    return (f"Risk is {v['risk_level']} and {trend}, with the fatigue probability at "
            f"{v['fatigue_probability_percent']}%. " + "; ".join(v["findings"]) + ".")


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def _groq_source() -> str:
    """The text of llm.py from the Groq section onward.

    Scoped deliberately: the Ollama client legitimately ships a default model
    name, so a whole-file check would be testing the wrong thing.
    """
    import fatigue_xai.llm as module
    text = open(module.__file__, encoding="utf-8").read()
    marker = "class GroqClient"
    assert marker in text
    return text[text.index(marker):]


def test_no_model_is_hard_coded_and_no_credential_is_in_source():
    """The base URL is a default; the key and the model must come from config."""
    import fatigue_xai.llm as module

    whole = open(module.__file__, encoding="utf-8").read()
    assert KEY not in whole, "a credential must never appear in source"

    # The endpoint default is a documented, non-secret constant.
    assert GROQ_DEFAULT_BASE_URL == "https://api.groq.com/openai/v1"

    src = _groq_source()
    # The client must reference the constant, not repeat the URL inline.
    assert "GROQ_DEFAULT_BASE_URL" in src
    assert "https://" not in src, "no endpoint URL should be inlined in the client"
    assert "GROQ_MODEL" in src, "the model must be read from configuration"

    # No plausible Groq model id is baked in as a fallback.
    for model in ("llama-3.1", "llama-3.3", "mixtral", "gemma", "qwen"):
        assert model not in src, f"{model!r} looks like a hard-coded model name"
    assert "os.getenv(\"GROQ_MODEL\")" in whole


def test_a_fully_configured_client_is_enabled():
    c = client(answering())
    assert c.is_configured() and c.config_problem() is None
    st = c.status()
    assert st.enabled and st.model == "test-model"
    assert c.name == "groq:test-model"


def test_missing_key_is_a_degraded_state_not_a_crash():
    """A missing credential must not stop the service — it must stop the LLM."""
    c = GroqClient(api_key="", model="test-model", transport=httpx.MockTransport(answering()))
    assert not c.is_configured()
    assert "GROQ_API_KEY" in (c.config_problem() or "")
    with pytest.raises(LLMError) as excinfo:
        run(_collect(c))
    assert excinfo.value.kind == ErrorKind.NOT_CONFIGURED
    assert excinfo.value.status is None and excinfo.value.retryable is False


def test_missing_model_is_reported_by_name():
    c = GroqClient(api_key=KEY, model=None, transport=httpx.MockTransport(answering()))
    assert not c.is_configured()
    assert "GROQ_MODEL" in (c.config_problem() or "")


def test_defaults_and_env_override(monkeypatch):
    monkeypatch.setenv("GROQ_BASE_URL", "https://example.invalid/v1/")
    monkeypatch.setenv("GROQ_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("GROQ_MAX_TOKENS", "64")
    monkeypatch.setenv("GROQ_MODEL", "from-env")
    c = GroqClient(api_key=KEY)
    assert c.base_url == "https://example.invalid/v1"  # trailing slash trimmed
    assert c.timeout_s == 3.5 and c.max_tokens == 64 and c.model == "from-env"


def test_invalid_numeric_configuration_fails_fast(monkeypatch):
    """A typo in the setting that bounds latency must not be silently defaulted."""
    monkeypatch.setenv("GROQ_TIMEOUT_SECONDS", "soon")
    with pytest.raises(ConfigError) as excinfo:
        GroqClient(api_key=KEY, model="m")
    assert "GROQ_TIMEOUT_SECONDS" in str(excinfo.value)

    monkeypatch.setenv("GROQ_TIMEOUT_SECONDS", "0")
    with pytest.raises(ConfigError):
        GroqClient(api_key=KEY, model="m")

    monkeypatch.delenv("GROQ_TIMEOUT_SECONDS")
    monkeypatch.setenv("GROQ_MAX_TOKENS", "-5")
    with pytest.raises(ConfigError) as excinfo:
        GroqClient(api_key=KEY, model="m")
    assert "GROQ_MAX_TOKENS" in str(excinfo.value)


def test_documented_defaults_are_used_when_unset(monkeypatch):
    for name in ("GROQ_BASE_URL", "GROQ_TIMEOUT_SECONDS", "GROQ_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    c = GroqClient(api_key=KEY, model="m")
    assert c.base_url == GROQ_DEFAULT_BASE_URL
    assert c.timeout_s == GROQ_DEFAULT_TIMEOUT_S
    assert c.max_tokens == GROQ_DEFAULT_MAX_TOKENS


@pytest.mark.parametrize("kind,expected", [
    ("none", None), ("", None), ("off", None), ("disabled", None),
    ("groq", "groq"), ("openai", "openai-compat"), ("ollama", "ollama"),
    ("GROQ", "groq"), ("  groq  ", "groq"),
])
def test_provider_selection_from_env(monkeypatch, kind, expected):
    monkeypatch.setenv("NARRATIVE_LLM", kind)
    client_or_none = llm_from_env()
    if expected is None:
        assert client_or_none is None
    else:
        assert client_or_none.name.startswith(expected)


def test_an_unknown_provider_is_a_configuration_error(monkeypatch):
    monkeypatch.setenv("NARRATIVE_LLM", "gpt5turbo")
    with pytest.raises(ConfigError) as excinfo:
        llm_from_env()
    assert "gpt5turbo" in str(excinfo.value)
    assert "none, groq, openai, ollama" in str(excinfo.value)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

def test_a_successful_response_streams_its_chunks():
    c = client(lambda r: httpx.Response(200, content=sse("Risk ", "is ", "HIGH.")))
    assert "".join(run(_collect(c))) == "Risk is HIGH."
    assert c.status().calls == 1, "one logical call despite three chunks"
    st = c.status()
    assert st.calls == 1 and st.failures == 0 and st.last_ok_at is not None
    assert st.last_error_kind is None


def test_the_request_is_openai_compatible_and_deterministic():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=sse("ok"))

    run(_collect(client(handler, max_tokens=77, seed=7)))
    assert seen["url"] == f"{GROQ_DEFAULT_BASE_URL}/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["stream"] is True
    assert seen["body"]["temperature"] == 0 and seen["body"]["seed"] == 7
    assert seen["body"]["max_tokens"] == 77


def test_context_is_sent_through_unchanged():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=sse("ok"))

    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "FACTS:\n{}"}]
    c = client(handler)
    asyncio.run(_collect(c, messages))
    assert seen["messages"] == messages


# ---------------------------------------------------------------------------
# the error matrix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,kind", [
    (400, ErrorKind.BAD_REQUEST),
    (401, ErrorKind.UNAUTHORIZED),
    (403, ErrorKind.FORBIDDEN),
    (404, ErrorKind.NOT_FOUND),
    (422, ErrorKind.BAD_REQUEST),
    (429, ErrorKind.RATE_LIMITED),
    (500, ErrorKind.SERVER_ERROR),
    (502, ErrorKind.SERVER_ERROR),
    (503, ErrorKind.SERVER_ERROR),
])
def test_http_errors_are_classified_by_status(status, kind):
    c = client(erroring(status), max_attempts=1)
    with pytest.raises(LLMError) as excinfo:
        run(_collect(c))
    assert excinfo.value.kind == kind
    assert excinfo.value.status == status
    assert f"HTTP {status}" in str(excinfo.value)
    assert c.status().last_error_kind == kind


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried(status):
    """A bad key or model fails identically next time; retrying only adds latency."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"error": {"message": "nope"}})

    c = client(handler, max_attempts=3)
    with pytest.raises(LLMError) as excinfo:
        run(_collect(c))
    assert excinfo.value.retryable is False
    assert len(calls) == 1, "a deterministic 4xx must be attempted exactly once"


def test_rate_limiting_is_retried_within_the_bound():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, content=sse("recovered"))

    c = client(handler, max_attempts=2)
    assert "".join(run(_collect(c))) == "recovered"
    assert len(calls) == 2


def test_retries_are_bounded():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    c = client(handler, max_attempts=2)
    with pytest.raises(LLMError) as excinfo:
        run(_collect(c))
    assert excinfo.value.kind == ErrorKind.SERVER_ERROR
    assert len(calls) == 2, "the retry budget must be honoured exactly"


def test_a_successful_retry_does_not_duplicate_the_budget():
    c = client(answering("fine"), max_attempts=5)
    assert "".join(run(_collect(c))) == "fine"
    assert c.status().calls == 1


def test_a_timeout_is_classified_and_retried_once():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("no response", request=request)

    c = client(handler, max_attempts=2)
    with pytest.raises(LLMError) as excinfo:
        run(_collect(c))
    assert excinfo.value.kind == ErrorKind.TIMEOUT and excinfo.value.retryable
    assert len(calls) == 2
    assert "did not respond within" in str(excinfo.value)


def test_a_connection_failure_is_classified():
    def handler(request):
        raise httpx.ConnectError("name resolution failed", request=request)

    c = client(handler, max_attempts=1)
    with pytest.raises(LLMError) as excinfo:
        run(_collect(c))
    assert excinfo.value.kind == ErrorKind.CONNECTION
    assert "cannot reach Groq" in str(excinfo.value)


def test_no_text_at_all_is_an_empty_response():
    """A 200 with only ``[DONE]`` is not success — it is a provider fault."""
    c = client(lambda r: httpx.Response(200, content=b"data: [DONE]\n\n"), max_attempts=1)
    with pytest.raises(LLMError) as excinfo:
        run(_collect(c))
    assert excinfo.value.kind == ErrorKind.EMPTY_RESPONSE


def test_malformed_stream_json_is_a_provider_fault_not_an_index_error():
    body = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\ndata: {not json\n\n'
    c = client(lambda r: httpx.Response(200, content=body), max_attempts=1)
    with pytest.raises(LLMError) as excinfo:
        run(_collect(c))
    assert excinfo.value.kind == ErrorKind.MALFORMED_RESPONSE


def test_a_truncated_stream_shape_is_classified():
    """``choices`` missing is malformed, not a KeyError leaking to the caller."""
    body = b'data: {"id":"x","object":"chat.completion.chunk"}\n\n'
    c = client(lambda r: httpx.Response(200, content=body), max_attempts=1)
    with pytest.raises(LLMError) as excinfo:
        run(_collect(c))
    assert excinfo.value.kind == ErrorKind.MALFORMED_RESPONSE


def test_a_failure_after_partial_output_is_not_retried():
    """Retrying mid-sentence would duplicate words in the accepted narrative."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"Risk is"}}]}\n\ndata: {broken\n\n')

    c = client(handler, max_attempts=3)
    with pytest.raises(LLMError) as excinfo:
        run(_collect(c))
    assert len(calls) == 1, "no re-request once text has been emitted"


def test_an_unparseable_error_body_does_not_break_classification():
    c = client(lambda r: httpx.Response(503, content=b"<html>gateway</html>"), max_attempts=1)
    with pytest.raises(LLMError) as excinfo:
        run(_collect(c))
    assert excinfo.value.kind == ErrorKind.SERVER_ERROR
    assert "unparseable body" in str(excinfo.value)
    assert "<html>" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------

def test_the_api_key_never_appears_in_a_message_or_a_status():
    """Providers echo request details in error bodies; the key must not survive."""
    c = client(lambda r: httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}}),
               max_attempts=1)
    with pytest.raises(LLMError) as excinfo:
        run(_collect(c))
    assert KEY not in str(excinfo.value)
    assert KEY not in json.dumps(c.status().as_dict())


def test_redact_replaces_only_the_secret():
    assert redact(f"key={KEY}", KEY) == "key=***"
    assert redact("no secret here", KEY) == "no secret here"
    assert redact("anything", None) == "anything"


def test_status_never_carries_the_key():
    from fatigue_xai.llm import ProviderStatus
    payload = ProviderStatus(enabled=True, model="m", reason=None).as_dict()
    assert KEY not in json.dumps(payload)
    assert set(payload) == {
        "enabled", "model", "reason", "calls", "failures",
        "last_error_kind", "last_error_at", "last_error_message", "last_ok_at",
    }


# ---------------------------------------------------------------------------
# classification helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,kind", [
    (401, ErrorKind.UNAUTHORIZED), (403, ErrorKind.FORBIDDEN), (404, ErrorKind.NOT_FOUND),
    (429, ErrorKind.RATE_LIMITED), (400, ErrorKind.BAD_REQUEST), (500, ErrorKind.SERVER_ERROR),
    (599, ErrorKind.SERVER_ERROR),
])
def test_kind_from_status(status, kind):
    assert kind_from_status(status) == kind


def test_classify_falls_back_to_the_exception_name():
    class Weird(Exception):
        pass
    assert classify(Weird()) == "Weird"
    assert classify(asyncio.TimeoutError()) == ErrorKind.TIMEOUT
    assert classify(LLMError(ErrorKind.RATE_LIMITED, "x")) == ErrorKind.RATE_LIMITED


# ---------------------------------------------------------------------------
# narrative integration — the part that matters
# ---------------------------------------------------------------------------

def test_a_grounded_groq_answer_becomes_the_final_narrative():
    a = assessment_at(184)
    assert a.risk_level == "HIGH"
    service = NarrativeService(client(answering(grounded_answer(a))))
    ev = run(service.narrate(a))
    assert ev["source"] == SOURCE_LLM and ev["llm_status"] == LLM_OK
    assert ev["guard"]["passed"] is True
    assert ev["provider"] == "groq:test-model"
    assert ev["error_kind"] is None


def test_groq_failure_keeps_the_assessment_and_falls_back():
    """The defining requirement: an LLM outage is not a detection outage."""
    a = assessment_at(184)
    service = NarrativeService(client(erroring(503), max_attempts=1))
    ev = run(service.narrate(a))
    assert ev["source"] == SOURCE_FALLBACK
    assert ev["llm_status"] == LLM_UNAVAILABLE
    assert ev["error_kind"] == ErrorKind.SERVER_ERROR
    assert ev["risk_level"] == a.risk_level, "the deterministic verdict is unchanged"
    assert ev["text"].startswith(f"[{a.ts_str}] HIGH RISK - "), "an explanation is still delivered"
    assert ev["guard"]["passed"] is False


def test_a_missing_key_falls_back_without_touching_the_network():
    a = assessment_at(184)
    calls = []

    def handler(request):  # pragma: no cover - must not run
        calls.append(1)
        return httpx.Response(200, content=sse("nope"))

    service = NarrativeService(GroqClient(api_key="", model="m",
                                          transport=httpx.MockTransport(handler)))
    ev = run(service.narrate(a))
    assert ev["source"] == SOURCE_FALLBACK
    assert ev["llm_status"] == LLM_UNAVAILABLE
    assert ev["error_kind"] == ErrorKind.NOT_CONFIGURED
    assert calls == [], "an unconfigured provider must not be called at all"


def test_a_hallucinating_groq_answer_is_rejected_and_replaced():
    a = assessment_at(184)
    # Confident, fluent, and entirely unsupported: 91% is not in the facts, and
    # 'heart rate' is a signal this system does not measure.
    bad = "Risk is LOW. Heart rate is 91 bpm and the driver is medically unfit to drive."
    service = NarrativeService(client(answering(bad), max_attempts=1))
    ev = run(service.narrate(a))
    assert ev["source"] == SOURCE_FALLBACK
    assert ev["llm_status"] == LLM_GUARD_REJECTED
    assert ev["error_kind"] is None, "this is a guard decision, not a provider failure"
    assert "LOW" not in ev["text"].split("RISK - ")[1].split(".")[0]
    assert ev["guard"]["violations"]


def test_critical_risk_bypasses_the_provider_entirely():
    """A CRITICAL verdict must not wait on a network round trip."""
    a = assessment_at(184).model_copy(update={"risk_level": "CRITICAL"})
    calls = []

    def handler(request):  # pragma: no cover - must not run
        calls.append(1)
        return httpx.Response(200, content=sse("should never be used"))

    service = NarrativeService(client(handler))
    ev = run(service.narrate(a))
    assert calls == [], "no request may be attempted for a CRITICAL assessment"
    assert ev["source"] == SOURCE_TEMPLATE
    assert ev["llm_status"] == LLM_SKIPPED
    assert ev["text"].startswith(f"[{a.ts_str}] CRITICAL RISK - ")


def test_no_provider_reports_none_rather_than_a_blank():
    a = assessment_at(184)
    service = NarrativeService(None)
    ev = run(service.narrate(a))
    assert ev["provider"] is None
    assert ev["source"] == SOURCE_TEMPLATE
    assert ev["llm_status"] == LLM_DISABLED
    assert service.provider_status() is None


def test_provider_status_is_exposed_for_the_health_endpoint():
    a = assessment_at(184)
    service = NarrativeService(client(answering(grounded_answer(a))))
    run(service.narrate(a))
    st = service.provider_status()
    assert st is not None and st.calls == 1 and st.failures == 0
    assert st.last_ok_at is not None
    assert st.enabled and st.model == "test-model"
    assert st.as_dict()["model"] == "test-model"


def test_a_failed_call_is_visible_in_provider_status():
    service = NarrativeService(client(erroring(429), max_attempts=1))
    run(service.narrate(assessment_at(184)))
    st = service.provider_status()
    assert st is not None and st.failures == 1
    assert st.last_error_kind == ErrorKind.RATE_LIMITED
    assert st.last_error_at is not None


def test_every_narrative_event_carries_provider_and_status():
    """The UI must be able to label each event without inferring anything."""
    a = assessment_at(184)
    evs = run(_collect_events(NarrativeService(client(answering(grounded_answer(a)))), a))
    for ev in evs:
        assert "provider" in ev, ev
    final = evs[-1]
    assert final["type"] == "narrative_final" and final["llm_status"] == LLM_OK
    assert final["source"] == SOURCE_LLM
    assert [e["type"] for e in evs][0] == "narrative_start"
    assert evs[0]["provider"] == "groq:test-model"
    assert evs[0]["llm_status"] == "not_called"


# ---------------------------------------------------------------------------
# async helpers
# ---------------------------------------------------------------------------

async def _collect(c: GroqClient, messages=None):
    if messages is None:
        messages = [{"role": "user", "content": "hi"}]
    return [piece async for piece in c.stream(messages)]


async def _collect_events(service, a):
    return [ev async for ev in service.stream(a)]
