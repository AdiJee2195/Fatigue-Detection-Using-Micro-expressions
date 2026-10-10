"""The Python side of the narrative error-reason contract.

Companions:
  * ``gateway/.../dto/NarrativeErrorReasonsTest.java``
  * ``frontend/tests/errorContract.test.ts``

The three sets must be identical. They were not, and the failure was invisible
because an unrecognised reason degrades into a generic message instead of
throwing.
"""
import json
import re

import pytest
from fastapi.testclient import TestClient

from fatigue_xai.api import build_registry, create_app
from fatigue_xai.errors import (
    ALL_REASONS,
    ENGINE_REASONS,
    REASON_INVALID_TELEMETRY_FRAME,
    REASON_MALFORMED_EVENT,
    REASON_MISSING_TYPE,
    REASON_NARRATIVE_UNAVAILABLE,
    REASON_UNKNOWN_TYPE,
)

CANONICAL = {
    "invalid_telemetry_frame",
    "missing_type",
    "unknown_type",
    "narrative_service_unavailable",
    "malformed_narrative_event",
}

_CODE = re.compile(r"^[a-z][a-z0-9_]*$")


def test_theSetMatchesTheCanonicalVocabulary():
    assert set(ALL_REASONS) == CANONICAL


def test_onlyTheEngineOwnedSubsetCanOriginateHere():
    """The gateway mints the rest; the engine must not invent them."""
    assert set(ENGINE_REASONS) <= CANONICAL
    assert ENGINE_REASONS == {REASON_INVALID_TELEMETRY_FRAME}


def test_everyReasonIsACodeNotProse():
    for reason in ALL_REASONS:
        assert _CODE.match(reason), f"{reason!r} is not a canonical code"
        assert " " not in reason


def test_theModuleConstantValuesAreTheCanonicalStrings():
    assert REASON_INVALID_TELEMETRY_FRAME == "invalid_telemetry_frame"
    assert REASON_MISSING_TYPE == "missing_type"
    assert REASON_UNKNOWN_TYPE == "unknown_type"
    assert REASON_NARRATIVE_UNAVAILABLE == "narrative_service_unavailable"
    assert REASON_MALFORMED_EVENT == "malformed_narrative_event"


def test_theOldProseReasonIsGone():
    """'invalid telemetry frame' with spaces was the bug: it matched nothing."""
    assert "invalid telemetry frame" not in ALL_REASONS


@pytest.mark.parametrize("payload", [
    "not json at all",
    json.dumps({"class_probs": {"nope": 1.0}}),      # unknown labels
    json.dumps({"class_probs": {"microsleep": 1.0}}),  # missing ts
])
def test_a_rejected_frame_reportsTheCanonicalCode(payload):
    """End to end over the real WebSocket: the code the gateway will relay."""
    reg = build_registry(use_env_llm=False)
    with TestClient(create_app(reg)) as client:
        with client.websocket_connect("/ws/contract") as ws:
            ws.send_text(payload)
            event = ws.receive_json()
    assert event["type"] == "error"
    assert event.get("reason") == REASON_INVALID_TELEMETRY_FRAME
    assert event.get("reason") in CANONICAL


def test_theEngineStillSendsSomethingAHumanCanRead():
    """A code the UI switches on, plus prose a log reader can act on."""
    reg = build_registry(use_env_llm=False)
    with TestClient(create_app(reg)) as client:
        with client.websocket_connect("/ws/contract") as ws:
            ws.send_text("not json")
            event = ws.receive_json()
    assert event["reason"] == REASON_INVALID_TELEMETRY_FRAME
    assert "message" in event and event["message"]
    assert event["message"] != event["reason"], "the code and the prose are different fields"
