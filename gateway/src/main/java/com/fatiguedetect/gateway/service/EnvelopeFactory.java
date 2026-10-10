package com.fatiguedetect.gateway.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.fatiguedetect.gateway.dto.EnvelopeType;
import com.fatiguedetect.gateway.dto.NarrativeEventType;
import com.fatiguedetect.gateway.dto.WebSocketEnvelope;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

/**
 * Converts a narrative-service WebSocket event into the gateway's envelope.
 *
 * The events are relayed with their payload intact: the gateway lifts {@code type}
 * to the envelope level and drops it from the payload, but never reinterprets the
 * facts (risk level, findings, evidence). Those come from the deterministic rule
 * engine and the guard on the Python side, and the gateway must not be able to
 * change them.
 *
 * The event vocabulary lives in {@link NarrativeEventType} — this class only
 * decides what to do when a name is not recognised.
 */
@Slf4j
@Component
public class EnvelopeFactory {

    private final ObjectMapper objectMapper;

    public EnvelopeFactory(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    public WebSocketEnvelope fromPythonEvent(String sessionId, String json) {
        JsonNode root;
        try {
            root = objectMapper.readTree(json);
        } catch (Exception e) {
            log.warn("sessionId={} malformed narrative event: {}", sessionId, e.getMessage());
            return WebSocketEnvelope.narrativeError(sessionId, EnvelopeType.REASON_MALFORMED_EVENT);
        }
        if (root == null || !root.isObject()) {
            log.warn("sessionId={} narrative event was not a JSON object", sessionId);
            return WebSocketEnvelope.narrativeError(sessionId, EnvelopeType.REASON_MALFORMED_EVENT);
        }

        JsonNode typeNode = root.get("type");
        String wireName = (typeNode != null && typeNode.isTextual()) ? typeNode.asText() : null;
        if (wireName == null || wireName.isBlank()) {
            log.warn("sessionId={} narrative event has no type", sessionId);
            return WebSocketEnvelope.narrativeError(sessionId, EnvelopeType.REASON_MISSING_TYPE);
        }

        // Payload = the whole event minus the discriminator, which the envelope carries.
        ObjectNode payload = ((ObjectNode) root).deepCopy();
        payload.remove("type");

        return NarrativeEventType.fromWire(wireName)
                .map(event -> relay(sessionId, event, payload))
                .orElseGet(() -> unknown(sessionId, wireName, payload));
    }

    private WebSocketEnvelope relay(String sessionId, NarrativeEventType event, ObjectNode payload) {
        if (event == NarrativeEventType.ERROR && !payload.hasNonNull("reason")) {
            // The engine reports a rejected *frame* here. Mint the canonical code.
            //
            // This used to promote the engine's `message` field into `reason`,
            // which put prose ("invalid telemetry frame", with spaces) where the
            // dashboard expects a code, so it never matched the declared union. A
            // human-readable message stays where it belongs, under `message`.
            payload.put("reason", EnvelopeType.REASON_INVALID_TELEMETRY_FRAME);
        }
        return WebSocketEnvelope.of(event.envelopeType(), sessionId, payload);
    }

    private WebSocketEnvelope unknown(String sessionId, String wireName, ObjectNode payload) {
        log.debug("sessionId={} unknown narrative event type={} — relaying as narrative_error",
                sessionId, wireName);
        payload.put("reason", EnvelopeType.REASON_UNKNOWN_TYPE);
        payload.put("original_type", wireName);
        return WebSocketEnvelope.of(EnvelopeType.NARRATIVE_ERROR, sessionId, payload);
    }
}
