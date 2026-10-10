package com.fatiguedetect.gateway.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fatiguedetect.gateway.dto.EnvelopeType;
import com.fatiguedetect.gateway.dto.WebSocketEnvelope;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The gateway relays narrative events without reinterpreting them: the type is
 * lifted to the envelope, the payload is passed through unchanged, and anything
 * unrecognised degrades to narrative_error rather than reaching the UI as-is.
 */
class EnvelopeFactoryTest {

    private final ObjectMapper objectMapper = new ObjectMapper();
    private final EnvelopeFactory factory = new EnvelopeFactory(objectMapper);

    @Test
    void liftsTypeAndStripsItFromThePayload() {
        WebSocketEnvelope envelope = factory.fromPythonEvent("op-1",
                "{\"type\":\"assessment\",\"session_id\":\"op-1\",\"assessment\":{\"risk_level\":\"LOW\"}}");

        assertEquals("assessment", envelope.getType());
        assertEquals("op-1", envelope.getSessionId());
        assertTrue(envelope.getGatewayTimestamp() > 0);

        JsonNode payload = objectMapper.valueToTree(envelope.getPayload());
        assertFalse(payload.has("type"), "the discriminator must not be duplicated inside the payload");
        assertEquals("LOW", payload.get("assessment").get("risk_level").asText());
        assertEquals("op-1", payload.get("session_id").asText(), "the rest of the event is preserved");
    }

    @Test
    void preservesEveryAuthoritativeFieldOfANarrativeFinal() {
        WebSocketEnvelope envelope = factory.fromPythonEvent("op-1",
                "{\"type\":\"narrative_final\",\"narrative_id\":\"abc\",\"text\":\"Risk is HIGH.\","
                        + "\"source\":\"template\",\"guard\":{\"passed\":true,\"violations\":[],\"attempts\":0},"
                        + "\"latency_ms\":1.5,\"risk_level\":\"HIGH\"}");

        assertEquals("narrative_final", envelope.getType());
        JsonNode payload = objectMapper.valueToTree(envelope.getPayload());
        assertEquals("Risk is HIGH.", payload.get("text").asText());
        assertEquals("template", payload.get("source").asText(), "the source drives the UI 'not LLM' badge");
        assertTrue(payload.get("guard").get("passed").asBoolean());
        assertEquals("HIGH", payload.get("risk_level").asText());
    }

    @Test
    void relaysProvisionalChunksAndCancellations() {
        assertEquals("narrative_chunk",
                factory.fromPythonEvent("op-1", "{\"type\":\"narrative_chunk\",\"text\":\"partial\"}").getType());
        assertEquals("narrative_cancelled",
                factory.fromPythonEvent("op-1", "{\"type\":\"narrative_cancelled\"}").getType());
        assertEquals("narrative_retry",
                factory.fromPythonEvent("op-1", "{\"type\":\"narrative_retry\",\"violations\":[\"x\"]}").getType());
    }

    @Test
    void mapsAPythonFrameErrorToNarrativeError() {
        // The engine now sends the canonical code itself; the prose stays under
        // `message`. Asserting the code (not the prose) is the point: `reason` is
        // the field the dashboard switches on.
        WebSocketEnvelope envelope = factory.fromPythonEvent("op-1",
                "{\"type\":\"error\",\"reason\":\"invalid_telemetry_frame\","
                        + "\"message\":\"invalid telemetry frame\",\"detail\":\"ts missing\"}");

        assertEquals("narrative_error", envelope.getType(), "\"error\" is not a type the client knows");
        JsonNode payload = objectMapper.valueToTree(envelope.getPayload());
        assertEquals(EnvelopeType.REASON_INVALID_TELEMETRY_FRAME, payload.get("reason").asText());
        assertEquals("invalid telemetry frame", payload.get("message").asText(),
                "the human-readable message must survive for the UI to show");
        assertEquals("ts missing", payload.get("detail").asText());
        assertFalse(payload.has("type"));
    }

    @Test
    void anEngineErrorWithoutAReasonStillGetsACanonicalCode() {
        // Back-compat with an engine build that predates the codes: prose must not
        // be promoted into `reason`, or the dashboard cannot match it.
        WebSocketEnvelope envelope = factory.fromPythonEvent("op-1",
                "{\"type\":\"error\",\"message\":\"invalid telemetry frame\"}");

        JsonNode payload = objectMapper.valueToTree(envelope.getPayload());
        assertEquals(EnvelopeType.REASON_INVALID_TELEMETRY_FRAME, payload.get("reason").asText());
        assertFalse(payload.get("reason").asText().contains(" "),
                "a reason must be a code, never prose");
    }

    @Test
    void unknownTypeBecomesNarrativeError() {
        WebSocketEnvelope envelope = factory.fromPythonEvent("op-1", "{\"type\":\"telemetry_v2\"}");

        assertEquals("narrative_error", envelope.getType());
        JsonNode payload = objectMapper.valueToTree(envelope.getPayload());
        assertEquals(EnvelopeType.REASON_UNKNOWN_TYPE, payload.get("reason").asText());
        assertEquals("telemetry_v2", payload.get("original_type").asText());
    }

    @Test
    void malformedInputBecomesNarrativeError() {
        assertEquals("narrative_error", factory.fromPythonEvent("op-1", "{not json").getType());
        assertEquals("narrative_error", factory.fromPythonEvent("op-1", "[]").getType());
        assertEquals("narrative_error", factory.fromPythonEvent("op-1", "{\"no_type\":1}").getType());
        assertEquals("narrative_error", factory.fromPythonEvent("op-1", "\"just a string\"").getType());
        assertEquals("narrative_error", factory.fromPythonEvent("op-1", "").getType());
    }
}
