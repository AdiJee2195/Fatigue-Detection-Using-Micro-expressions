package com.fatiguedetect.gateway.dto;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.Map;

/**
 * Outbound frame for the narrative XAI WebSocket (POST-equivalent per frame).
 *
 * Contract (xai_narrative/fatigue_xai/schemas.py :: FrameTelemetry):
 *   - session_id comes from the WebSocket URL on the Python side, so it is NOT sent.
 *   - ts is epoch SECONDS and must be strictly increasing per session.
 *   - only ts + class_probs are mandatory; nulls are omitted so the rule engine can
 *     distinguish "signal absent" (degrades gracefully) from "signal is zero".
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@JsonInclude(JsonInclude.Include.NON_NULL)
public class TelemetryFrame {

    /** Epoch SECONDS, strictly increasing within a session. */
    private double ts;

    @JsonProperty("class_probs")
    private Map<String, Double> classProbs;

    @JsonProperty("p_fatigue")
    private Double pFatigue;

    private Double ear;

    /** FACS intensity 0-5. */
    private Map<String, Double> au;

    @JsonProperty("face_detected")
    private Boolean faceDetected;

    private Double illumination;

    @JsonProperty("rppg_bpm")
    private Double rppgBpm;

    @JsonProperty("frame_id")
    private long frameId;

    /**
     * Pipeline identity, relayed verbatim: {@code stub} or {@code onnx}.
     *
     * <p>The gateway does not interpret this value. Interpreting it is the vision
     * service's job (it is the only component that knows whether a model ran on a
     * real frame), so relaying the producer's own words is the only correct
     * behaviour here. Re-deriving it downstream would be a guess, and a guess is
     * how a synthetic number becomes a statement about a driver.
     */
    private String mode;

    /**
     * Canonical provenance: {@code synthetic}, {@code live} or {@code unknown}.
     *
     * <p>Without this hop the narrative engine cannot distinguish a demonstration
     * from an observation, and neither can the language model it briefs.
     */
    @JsonProperty("mode_class")
    private String modeClass;

    /** Per-metric provenance, e.g. {@code {ear: live, rppg_bpm: not_configured}}. */
    @JsonProperty("signal_status")
    private Map<String, String> signalStatus;

    /**
     * Projects a vision response onto the narrative telemetry contract.
     *
     * <p>Provenance is copied through unchanged. It is not defaulted and not
     * inferred: a vision service that omits it yields {@code null}, which the
     * engine reports as {@code unknown} rather than as a measurement.
     *
     * @param result  vision service response
     * @param frameId monotonic per-session frame counter
     */
    public static TelemetryFrame from(InferenceResult result, long frameId) {
        return TelemetryFrame.builder()
                .ts(result.getTs())
                .classProbs(result.getClassProbs())
                .pFatigue(result.getPFatigue())
                .ear(result.getEar())
                .au(result.getAu())
                .faceDetected(result.getFaceDetected() == null ? Boolean.TRUE : result.getFaceDetected())
                .illumination(result.getIllumination())
                .rppgBpm(result.getRppgBpm())
                .frameId(frameId)
                .mode(result.getMode())
                .modeClass(result.getModeClass())
                .signalStatus(result.getSignalStatus())
                .build();
    }
}
