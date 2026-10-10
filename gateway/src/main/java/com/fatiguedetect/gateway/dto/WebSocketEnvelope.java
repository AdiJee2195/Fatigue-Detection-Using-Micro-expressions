package com.fatiguedetect.gateway.dto;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * Tagged envelope for ALL outbound WebSocket messages.
 * The React client dispatches on 'type' to route payloads to different UI components.
 *
 * Types emitted by the gateway:
 *   telemetry, telemetry_error,
 *   assessment, narrative_start, narrative_chunk, narrative_retry, narrative_final,
 *   narrative_cancelled, narrative_error
 *
 * The narrative_* types are relayed verbatim from the Python narrative service
 * (see EnvelopeFactory.fromPythonEvent) — the gateway does not reinterpret them.
 */
@Data
@NoArgsConstructor
@AllArgsConstructor
@JsonInclude(JsonInclude.Include.NON_NULL)
public class WebSocketEnvelope {

    /** Discriminator the client uses to decide how to render the payload. */
    private String type;

    /** Identifies which operator/device session this message belongs to. */
    private String sessionId;

    /** Payload — structure depends on 'type' (a typed DTO or a raw JSON tree). */
    private Object payload;

    /** Gateway-side timestamp (epoch millis) when this envelope was created. */
    @JsonProperty("timestamp")
    private long gatewayTimestamp;

    // ---- Static factory methods ----

    /** Generic factory used for relayed narrative events and error envelopes. */
    public static WebSocketEnvelope of(String type, String sessionId, Object payload) {
        return new WebSocketEnvelope(type, sessionId, payload, System.currentTimeMillis());
    }

    public static WebSocketEnvelope telemetry(String sessionId, InferenceResult result) {
        return new WebSocketEnvelope(EnvelopeType.TELEMETRY, sessionId, result, System.currentTimeMillis());
    }

    public static WebSocketEnvelope telemetryError(String sessionId, String reason) {
        return new WebSocketEnvelope(EnvelopeType.TELEMETRY_ERROR, sessionId,
                new ErrorPayload(reason), System.currentTimeMillis());
    }

    public static WebSocketEnvelope narrativeError(String sessionId, String reason) {
        return new WebSocketEnvelope(EnvelopeType.NARRATIVE_ERROR, sessionId,
                new ErrorPayload(reason), System.currentTimeMillis());
    }

    @Data
    @AllArgsConstructor
    public static class ErrorPayload {
        private String reason;
    }
}
