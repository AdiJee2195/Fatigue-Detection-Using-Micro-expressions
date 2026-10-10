package com.fatiguedetect.gateway.dto;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/**
 * Incoming frame payload from the React client over WebSocket.
 * The client sends base64-encoded JPEG frames wrapped in JSON so we can
 * use TextWebSocketHandler (see VideoWebSocketHandler for the rationale).
 *
 * Also reused as the request body for the vision service
 * (POST /api/v1/analyze), where {@code sessionId} is filled in by the gateway.
 */
@Data
@JsonIgnoreProperties(ignoreUnknown = true)
public class FrameRequest {

    /** Base64-encoded JPEG frame (no data: prefix). */
    private String frame;

    /** Client-side timestamp (epoch MILLIS) when the frame was captured. */
    @JsonProperty("timestamp")
    private long clientTimestamp;

    /**
     * Operator/device session identifier. Not sent by the browser (it is in the
     * WebSocket URL); populated by the gateway so the vision service can log it.
     */
    @JsonProperty("sessionId")
    private String sessionId;
}
