"""Canonical narrative error reason codes: one vocabulary, three languages.

The gateway and the dashboard both switch on an error's ``reason``, and the
dashboard's list is a TypeScript union that is checked at compile time. So the set
has to be identical in Python, Java and TypeScript — a reason the gateway can emit
but the frontend does not declare is *worse* than no reason at all, because it
looks typed while never matching.

That is not hypothetical. Before this module existed:

============================  ==============================
 gateway emitted               frontend declared
============================  ==============================
 ``narrative_event_missing_type``   ``missing_type``
 ``unknown_narrative_event_type``   ``unknown_type``
 ``"invalid telemetry frame"``      ``invalid_telemetry_frame``
============================  ==============================

None of the three matched, all three fell through the dashboard's ``default``
branch, and the declared union was dead code. Nothing crashed, which is exactly
why it went unnoticed.

The counterparts are:
  * ``gateway/.../dto/EnvelopeType.java``
  * ``frontend/src/types.ts`` (``NarrativeErrorReason``)

Each side has a test that asserts the full set, so a rename on one side fails the
build instead of the dashboard.
"""

from __future__ import annotations

#: The engine could not accept a frame. Emitted by the WebSocket endpoint when
#: telemetry fails validation, and relayed by the gateway under ``narrative_error``.
REASON_INVALID_TELEMETRY_FRAME = "invalid_telemetry_frame"

#: A relayed narrative event had no ``type`` field, so the gateway could not route it.
REASON_MISSING_TYPE = "missing_type"

#: A relayed narrative event had a ``type`` the gateway's vocabulary does not know.
REASON_UNKNOWN_TYPE = "unknown_type"

#: The narrative WebSocket was unavailable, or dropped mid-session.
REASON_NARRATIVE_UNAVAILABLE = "narrative_service_unavailable"

#: The event was unparseable JSON or was not a JSON object.
REASON_MALFORMED_EVENT = "malformed_narrative_event"

#: Every reason the narrative path may report. Declared as a frozenset so the
#: cross-language contract tests can compare it as a set, without caring about
#: ordering or about names that do not cross the wire.
ALL_REASONS = frozenset({
    REASON_INVALID_TELEMETRY_FRAME,
    REASON_MISSING_TYPE,
    REASON_UNKNOWN_TYPE,
    REASON_NARRATIVE_UNAVAILABLE,
    REASON_MALFORMED_EVENT,
})

#: Errors emitted by the narrative service itself (as opposed to relayed by the
#: gateway). Kept separate because only this subset can originate from the engine.
ENGINE_REASONS = frozenset({REASON_INVALID_TELEMETRY_FRAME})
