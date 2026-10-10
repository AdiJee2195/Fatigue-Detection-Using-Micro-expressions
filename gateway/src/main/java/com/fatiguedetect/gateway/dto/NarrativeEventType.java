package com.fatiguedetect.gateway.dto;

import java.util.Locale;
import java.util.Optional;

/**
 * The single source of truth for the narrative event protocol.
 *
 * This used to be a set of string literals inside {@code EnvelopeFactory}, which
 * meant the gateway had to be edited whenever the Python engine learned a new
 * event. Centralising it here:
 *
 *   - keeps the two names of each event together (the name the engine emits, and
 *     the name the browser receives),
 *   - makes the version explicit, so a protocol change is a visible decision,
 *     and
 *   - lets unknown events degrade to a single, testable fallback instead of
 *     leaking to the UI untyped.
 *
 * Backward compatibility: the wire names are exactly the strings the narrative
 * engine already emits, and are deliberately NOT renamed (e.g.
 * {@code narrative_chunk} is the provisional-text event). Renaming them would
 * break every deployed engine, so the enum adds structure without changing the
 * protocol.
 *
 * @see EnvelopeType for gateway-originated messages
 */
public enum NarrativeEventType {

    /** Periodic deterministic facts (~every 2 s): level, trend, findings. */
    ASSESSMENT("assessment"),

    /** A narrative has started; carries the timestamp/level prefix and the mode. */
    NARRATIVE_START("narrative_start"),

    /** PROVISIONAL streamed text. Unvalidated — the UI must replace it on final. */
    NARRATIVE_CHUNK("narrative_chunk"),

    /** The factual guard rejected a draft; a silent retry follows. */
    NARRATIVE_RETRY("narrative_retry"),

    /** Authoritative text: validated LLM output or the deterministic template. */
    NARRATIVE_FINAL("narrative_final"),

    /** A more severe alert pre-empted this narrative. */
    NARRATIVE_CANCELLED("narrative_cancelled"),

    /**
     * The engine could not accept a frame. Surfaced to the browser as
     * {@code narrative_error} so it cannot be confused with a transport failure
     * of the narrative WebSocket.
     */
    ERROR("error");

    /**
     * Protocol version of the narrative event vocabulary. Bump only together with
     * both producers and consumers.
     */
    public static final int PROTOCOL_VERSION = 1;

    private final String wireName;

    NarrativeEventType(String wireName) {
        this.wireName = wireName;
    }

    /** The name as it appears on the wire from the narrative engine. */
    public String wireName() {
        return wireName;
    }

    /** The envelope {@code type} the browser receives for this event. */
    public String envelopeType() {
        return this == ERROR ? EnvelopeType.NARRATIVE_ERROR : wireName;
    }

    /** Case-insensitive lookup of an inbound event name. */
    public static Optional<NarrativeEventType> fromWire(String wireName) {
        if (wireName == null || wireName.isBlank()) {
            return Optional.empty();
        }
        String normalised = wireName.trim().toLowerCase(Locale.ROOT);
        for (NarrativeEventType type : values()) {
            if (type.wireName.equals(normalised)) {
                return Optional.of(type);
            }
        }
        return Optional.empty();
    }
}
