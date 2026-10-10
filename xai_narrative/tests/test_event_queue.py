"""Narrative delivery under a slow consumer.

The regression this covers is a silent one: the old overflow path dropped the
*oldest* event regardless of its type, so a ``narrative_final`` could be discarded
while provisional text stayed on screen. Nothing logged, nothing raised, and the
operator read unvalidated text indefinitely.
"""
import asyncio

import pytest

from fatigue_xai.events import DEFAULT_MAX_SLOTS, NarrativeQueue


def chunk(text):
    return {"type": "narrative_chunk", "text": text, "provisional": True}


def final(text="done"):
    return {"type": "narrative_final", "text": text, "source": "llm"}


def assessment():
    return {"type": "assessment", "risk_level": "HIGH"}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# basics
# ---------------------------------------------------------------------------

def test_events_come_out_in_order():
    q = NarrativeQueue()
    for e in (assessment(), chunk("a"), final()):
        q.put_nowait(e)
    assert [run(q.get())["type"] for _ in range(3)] == [
        "assessment", "narrative_chunk", "narrative_final"]


def test_get_awaits_until_something_arrives():
    q = NarrativeQueue()

    async def scenario():
        task = asyncio.create_task(q.get())
        await asyncio.sleep(0)          # let the waiter park
        assert not task.done(), "get() must not return early with an empty queue"
        q.put_nowait(final())
        return await asyncio.wait_for(task, timeout=1)

    assert run(scenario())["type"] == "narrative_final"


def test_close_releases_a_waiter_with_none():
    q = NarrativeQueue()

    async def scenario():
        task = asyncio.create_task(q.get())
        await asyncio.sleep(0)
        q.close()
        return await asyncio.wait_for(task, timeout=1)

    assert run(scenario()) is None


def test_put_after_close_is_ignored():
    q = NarrativeQueue()
    q.close()
    q.put_nowait(final())
    assert len(q) == 0
    assert run(q.get()) is None


# ---------------------------------------------------------------------------
# the invariant that matters
# ---------------------------------------------------------------------------

def test_a_final_is_never_dropped_in_favour_of_provisional_text():
    """Overflow must evict replaceable events only."""
    q = NarrativeQueue(max_slots=8)
    q.put_nowait(final("authoritative"))
    for i in range(50):
        q.put_nowait(chunk(f"w{i} "))
    types = q.peek_types()
    assert "narrative_final" in types, types
    # And it is still retrievable, with its text intact.
    seen = []
    while len(q):
        seen.append(run(q.get()))
    finals = [e for e in seen if e["type"] == "narrative_final"]
    assert finals and finals[0]["text"] == "authoritative"


def test_provisional_chunks_are_coalesced_rather_than_lost():
    """A slow consumer loses inter-chunk timing, never text."""
    q = NarrativeQueue(max_slots=8)
    for i in range(40):
        q.put_nowait(chunk(f"{i},"))
    assert len(q) <= 8
    assert q.coalesced > 0

    text = ""
    while len(q):
        text += run(q.get())["text"]
    # Every fragment is present, in order: nothing was silently dropped.
    assert text == "".join(f"{i}," for i in range(40))


def test_coalescing_does_not_cross_a_retry_boundary():
    """A retry discards accumulated text; merging across it would resurrect it."""
    q = NarrativeQueue()
    q.put_nowait(chunk("stale draft"))
    q.put_nowait({"type": "narrative_retry", "violations": ["x"]})
    q.put_nowait(chunk("new draft"))
    assert [str(e["type"]) for e in [run(q.get()), run(q.get()), run(q.get())]] == [
        "narrative_chunk", "narrative_retry", "narrative_chunk"]


def test_coalescing_does_not_cross_a_provisional_boundary():
    """A final resets the stream; a later chunk must start a new slot."""
    q = NarrativeQueue()
    q.put_nowait(chunk("before "))
    q.put_nowait(final("authoritative"))
    q.put_nowait(chunk("after "))
    kinds = q.peek_types()
    assert kinds == ["narrative_chunk", "narrative_final", "narrative_chunk"]


def test_terminal_backlog_is_bounded_and_counted():
    """Memory must be bounded even against a producer of authoritative events."""
    q = NarrativeQueue(max_slots=4, max_terminal=4)
    for i in range(20):
        q.put_nowait(assessment())
    assert len(q) <= 4, "the slot budget still applies"
    assert q.dropped_terminal > 0
    assert q.stats()["dropped_terminal"] > 0


def test_replaceable_events_are_evicted_before_terminal_ones():
    q = NarrativeQueue(max_slots=3)
    q.put_nowait(chunk("early"))
    q.put_nowait(final("keep me"))
    q.put_nowait(assessment())
    q.put_nowait(chunk("late"))
    types = q.peek_types()
    assert "narrative_final" in types
    assert q.dropped_replaceable >= 1
    assert q.dropped_terminal == 0


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

def test_stats_report_the_policy_pressure():
    q = NarrativeQueue(max_slots=4)
    for i in range(30):
        q.put_nowait(chunk(str(i)))
    stats = q.stats()
    assert stats["buffered"] <= 4
    assert stats["coalesced"] > 0
    assert stats["max_slots"] == 4
    assert set(stats) == {
        "buffered", "coalesced", "dropped_replaceable", "dropped_terminal",
        "max_slots", "max_terminal",
    }


def test_the_default_budget_is_not_unbounded():
    q = NarrativeQueue()
    for i in range(DEFAULT_MAX_SLOTS * 4):
        q.put_nowait(chunk(str(i)))
    assert len(q) <= DEFAULT_MAX_SLOTS


@pytest.mark.parametrize("size", [0, 1, -5])
def test_a_degenerate_budget_still_behaves(size):
    """A misconfigured tiny budget must not deadlock or raise."""
    q = NarrativeQueue(max_slots=size)
    q.put_nowait(final())
    q.put_nowait(chunk("x"))
    assert len(q) >= 1
    assert run(q.get()) is not None


# ---------------------------------------------------------------------------
# integration: the WebSocket endpoint uses it
# ---------------------------------------------------------------------------

def test_the_endpoint_never_loses_a_final_when_the_client_is_slow():
    """End to end over the real endpoint, with the reader deliberately lagging.

    A realistic weak shape: the client reads nothing until the producer has
    finished, which is exactly the case the old policy mishandled.
    """
    from fastapi.testclient import TestClient

    from fatigue_xai.api import build_registry, create_app
    from fatigue_xai.llm import ScriptedLLM
    from fatigue_xai.simulator import generate

    registry = build_registry(llm=ScriptedLLM([""]), use_env_llm=False)
    frames, *_ = generate("drift_to_microsleep")
    with TestClient(create_app(registry)) as client:
        with client.websocket_connect("/ws/slow") as ws:
            for f in frames:
                if f.ts <= 190:
                    ws.send_text(f.model_dump_json())
            finals = 0
            for _ in range(4000):
                event = ws.receive_json()
                if event["type"] == "narrative_final":
                    finals += 1
                    assert event["text"].startswith("[")
                    if finals >= 1:
                        break
            assert finals >= 1, "a narrative_final must reach a slow client"
