package com.fatiguedetect.gateway.dto;

/**
 * Envelope {@code type} values that originate in the gateway itself.
 *
 * Pass-through narrative types are defined by {@link NarrativeEventType}; these
 * are the ones the gateway authors, so they live in one place instead of being
 * repeated as literals across the envelope factory, the orchestrator and the
 * narrative bridge.
 */
public final class EnvelopeType {

    /** Inference result for one frame. The most frequent message. */
    public static final String TELEMETRY = "telemetry";

    /** The vision service failed for this frame; the socket stays open. */
    public static final String TELEMETRY_ERROR = "telemetry_error";

    /** The narrative path degraded; telemetry is unaffected. Throttled. */
    public static final String NARRATIVE_ERROR = "narrative_error";

    // -----------------------------------------------------------------------
    // Reasons carried in an error payload.
    //
    // These strings are a three-language contract:
    //   - the Python engine emits them  (xai_narrative/fatigue_xai/errors.py)
    //   - the gateway relays or mints them (this file)
    //   - the dashboard switches on them (frontend/src/types.ts, NarrativeErrorReason)
    //
    // They used to disagree. The gateway sent "narrative_event_missing_type" and
    // "unknown_narrative_event_type" while the TypeScript union declared
    // "missing_type" and "unknown_type", so every one of them missed the typed
    // union and fell through the dashboard's default branch. Nothing broke — which
    // is precisely why it survived: an unmatched reason string degrades into a
    // generic message instead of an error.
    //
    // The Java values below are the canonical ones. Changing any of them means
    // changing all three files and their tests in the same commit;
    // {@code NarrativeErrorReasonsTest} fails the build otherwise.
    // -----------------------------------------------------------------------

    /** Relayed narrative event had no {@code type} field, so it could not be routed. */
    public static final String REASON_MISSING_TYPE = "missing_type";

    /** Relayed narrative event had a {@code type} outside the known vocabulary. */
    public static final String REASON_UNKNOWN_TYPE = "unknown_type";

    /** The engine rejected the telemetry frame (emitted by the engine, not here). */
    public static final String REASON_INVALID_TELEMETRY_FRAME = "invalid_telemetry_frame";

    /** The event was unparseable JSON or was not a JSON object. */
    public static final String REASON_MALFORMED_EVENT = "malformed_narrative_event";

    public static final String REASON_UNAVAILABLE = "narrative_service_unavailable";
    public static final String REASON_VISION_TIMEOUT = "vision_service_timeout";
    public static final String REASON_VISION_HTTP_ERROR = "vision_service_http_error";
    public static final String REASON_VISION_ERROR = "vision_service_error";

    private EnvelopeType() {
    }
}
