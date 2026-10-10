#!/usr/bin/env python3
"""Live Groq smoke test. Opt-in, and deliberately separate from the test suite.

Why this is not a pytest test
-----------------------------
Every unit test of the provider runs against a mocked HTTP transport, so the suite
needs no account, no network and no money. What a mock *cannot* prove is that the
real endpoint accepts this request shape, that the configured model exists, and
that a real key works. Those are genuine questions, and this script answers them
explicitly — once, on demand, with the result labelled as a live check rather than
mixed into the suite's pass count.

What it checks, in order
------------------------
1. Configuration is complete (key + model present).
2. The endpoint answers and the key is accepted.
3. The configured model is accepted (a wrong model is a 400/404, not a timeout).
4. A real request returns text, and the text passes the factual guard.
5. A deliberately malformed request fails in a classified way.

Rate limits and balance are reported as-is. A 429 here is a real answer about the
account, not a failure of the code.

Usage
-----
    export GROQ_API_KEY=...        # never commit this
    export GROQ_MODEL=...          # a model your account supports
    .venv/bin/python scripts/groq_smoke.py

Exit codes: 0 all checks passed, 1 a check failed, 2 the environment is incomplete.
Nothing printed here ever contains the API key.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "xai_narrative"))

from fatigue_xai.config import ConfigError  # noqa: E402
from fatigue_xai.guard import validate  # noqa: E402
from fatigue_xai.llm import ErrorKind, GroqClient, LLMError  # noqa: E402
from fatigue_xai.narrative import SOURCE_FALLBACK, SOURCE_LLM, NarrativeService  # noqa: E402
from fatigue_xai.prompt import facts_view  # noqa: E402
from fatigue_xai.rules import FacsRuleEngine  # noqa: E402
from fatigue_xai.simulator import generate  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def heading(text: str) -> None:
    print(f"\n=== {text} ===")


async def collect(client: GroqClient, messages) -> str:
    return "".join([piece async for piece in client.stream(messages)])


def assessment():
    """A real assessment from the replayed scenario, so the prompt is realistic."""
    frames, *_ = generate("drift_to_microsleep")
    engine = FacsRuleEngine("smoke")
    last = None
    for frame in frames:
        if frame.ts > 184:
            break
        last = engine.ingest(frame) or last
    return last


async def check_configuration() -> tuple[str, GroqClient | None]:
    heading("1. Configuration")
    try:
        client = GroqClient()
    except ConfigError as exc:
        print(f"{FAIL} invalid numeric configuration: {exc}")
        return FAIL, None

    problem = client.config_problem()
    if problem:
        print(f"{SKIP} {problem}")
        print("     export GROQ_API_KEY=... and GROQ_MODEL=... then re-run.")
        return SKIP, None

    print(f"{PASS} provider={client.name}")
    print(f"     base_url={client.base_url}")
    print(f"     timeout={client.timeout_s:g}s max_tokens={client.max_tokens}")
    print(f"     key=***{client.api_key[-4:]} (length {len(client.api_key)})")
    return PASS, client


async def check_reachability(client: GroqClient) -> tuple[str, bool]:
    heading("2. Reachability and credentials")
    up = await client.is_up()
    if up:
        print(f"{PASS} GET {client.base_url}/models answered 200")
        return PASS, True
    status = client.status()
    print(f"{FAIL} provider not reachable or credentials rejected "
          f"(last_error_kind={status.last_error_kind})")
    return FAIL, False


async def check_generation(client: GroqClient) -> str:
    heading("3. A real grounded explanation")
    a = assessment()
    if a is None:
        print(f"{FAIL} could not build an assessment")
        return FAIL

    service = NarrativeService(client)
    event = await service.narrate(a)
    print(f"     risk_level={event['risk_level']} source={event['source']} "
          f"llm_status={event['llm_status']}")
    if event.get("error_kind"):
        print(f"     error_kind={event['error_kind']}")
    print(f"     latency_ms={event['latency_ms']} first_token_ms={event['first_token_ms']}")
    print(f"     text: {event['text'][:300]}")

    if event["source"] == SOURCE_LLM and event["llm_status"] == "ok":
        print(f"{PASS} the provider wrote an explanation that passed the guard")
        return PASS
    if event["source"] == SOURCE_FALLBACK:
        kind = event.get("error_kind")
        print(f"{FAIL} the provider was not used (error_kind={kind})")
        if kind == ErrorKind.RATE_LIMITED:
            print("     This is a real answer about the account's rate limit, not a code fault.")
        if kind == ErrorKind.NOT_FOUND:
            print(f"     Check that GROQ_MODEL={client.model!r} exists on this account.")
        if kind == ErrorKind.UNAUTHORIZED:
            print("     The key was rejected. Check GROQ_API_KEY.")
        return FAIL
    print(f"{FAIL} unexpected source={event['source']}")
    return FAIL


async def check_guard_interlock(client: GroqClient) -> str:
    heading("4. The guard is independent of the provider's good behaviour")
    a = assessment()
    facts = facts_view(a)
    # A fluent answer that contradicts the facts must be rejected regardless of
    # who wrote it. This asserts the guard, not the model.
    bad = f"Risk is LOW. Heart rate is 91 bpm and the driver needs a medical assessment."
    result = validate(bad, facts)
    if result.passed:
        print(f"{FAIL} the guard accepted a contradicting answer")
        return FAIL
    print(f"{PASS} guard rejected it: {result.violations[:2]}")

    # And a live provider that hallucinates must land in the fallback path.
    original = client.stream

    async def hallucinating(messages):
        yield bad

    try:
        client.stream = hallucinating  # type: ignore[assignment]
        event = await NarrativeService(client).narrate(a)
    finally:
        client.stream = original  # type: ignore[assignment]

    if event["source"] == SOURCE_FALLBACK:
        print(f"{PASS} a hallucinating provider falls back to the template "
              f"(llm_status={event['llm_status']})")
        return PASS
    print(f"{FAIL} a hallucinating provider was published as source={event['source']}")
    return FAIL


async def check_error_classification(client: GroqClient) -> str:
    heading("5. Failures are classified, not guessed")
    results = [PASS]
    try:
        await collect(GroqClient(api_key=client.api_key, model="definitely-not-a-real-model-xyz",
                                 base_url=client.base_url, max_tokens=8, max_attempts=1),
                      [{"role": "user", "content": "hi"}])
        print(f"{FAIL} a nonexistent model was accepted")
        results.append(FAIL)
    except LLMError as exc:
        print(f"{PASS} unknown model -> kind={exc.kind} status={exc.status}")
    except Exception as exc:  # noqa: BLE001 - the point is that nothing raw escapes
        print(f"{FAIL} an unclassified {type(exc).__name__} escaped: {exc}")
        results.append(FAIL)
    return PASS if all(r == PASS for r in results) else FAIL


async def main() -> int:
    print("Live Groq smoke test")
    print("This performs real API calls against your account.")

    status, client = await check_configuration()
    if status == SKIP:
        print("\nResult: NOT RUN (configuration incomplete)")
        return 2
    if status != PASS or client is None:
        print("\nResult: FAILED")
        return 1

    outcomes = []
    reachable_status, reachable = await check_reachability(client)
    outcomes.append(reachable_status)
    if reachable:
        outcomes.append(await check_generation(client))
        outcomes.append(await check_guard_interlock(client))
        outcomes.append(await check_error_classification(client))
    else:
        print("\nSkipping generation checks: the provider was unreachable.")

    failed = [o for o in outcomes if o == FAIL]
    heading("Result")
    for name, value in zip(
            ["reachability", "generation", "guard interlock", "error classification"], outcomes):
        print(f"  {value:4} {name}")
    if failed:
        print("\nFAILED — see the diagnostics above. The service still runs: a provider "
              "outage degrades explanations, never the fatigue assessment.")
        return 1
    print("\nPASSED — the configured provider works end to end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
