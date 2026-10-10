package com.fatiguedetect.gateway.dto;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

import java.util.Map;

/**
 * Inference result returned by the Python vision service.
 *
 * The first group of fields is the telemetry contract consumed by the narrative
 * XAI service (see xai_narrative/fatigue_xai/schemas.py: FrameTelemetry).
 * The second group is what the React UI renders directly.
 *
 * Units and conventions that matter:
 *   - {@code ts}          : epoch SECONDS. Only ts + class_probs are mandatory downstream.
 *   - {@code classProbs}  : P(microsleep), P(neutral), P(yawning) — sums to ~1.
 *   - {@code au}          : FACS intensity 0-5, keyed like "AU4".
 *   - {@code auIntensities}: the same values normalised to 0-1 for UI gauges.
 *   - {@code ear}         : Eye Aspect Ratio (may be null until the EVM stage exists).
 *
 * Three provenance fields are relayed verbatim so the dashboard AND the narrative
 * engine can tell a real measurement from a synthetic one:
 *   - {@code mode}         : "stub" | "onnx" — which code path produced this row.
 *   - {@code modeClass}    : "synthetic" | "live" — were these numbers measured?
 *   - {@code signalStatus} : per-metric source, e.g. {"ear": "live",
 *                            "rppg_bpm": "not_configured"}.
 *
 * All three are nullable: an older vision service that does not send them must not
 * break anything, but the UI then has to say "unknown" rather than assume LIVE.
 * The gateway deliberately does not invent a value for any of them.
 *
 * {@code modeClass} used to be dropped on the hop to the narrative service, which
 * meant the engine could not know that its inputs were generated and would happily
 * describe a simulation as an observation of a driver. It is part of the wire
 * contract now; see {@link TelemetryFrame#from}.
 */
@Data
@JsonIgnoreProperties(ignoreUnknown = true)
public class InferenceResult {

    // ---- telemetry contract for the narrative engine ----

    /** Epoch SECONDS of the frame. Rewritten by the gateway to be strictly increasing. */
    private double ts;

    @JsonProperty("class_probs")
    private Map<String, Double> classProbs;

    /** P(fatigue) = P(microsleep) + P(yawning). */
    @JsonProperty("p_fatigue")
    private Double pFatigue;

    /** Eye Aspect Ratio; null when the EVM/optical-flow stage is not wired in. */
    private Double ear;

    /** FACS Action Unit intensities, 0-5, e.g. {"AU4": 2.1, "AU43": 0.5}. */
    private Map<String, Double> au;

    @JsonProperty("face_detected")
    private Boolean faceDetected = Boolean.TRUE;

    /** Mean face-ROI luminance, 0-1. */
    private Double illumination;

    /** Heart rate from rPPG; null until that stage exists (the guard rejects ungrounded mentions). */
    @JsonProperty("rppg_bpm")
    private Double rppgBpm;

    // ---- fields the React UI renders ----

    /** Risk score driving the dashboard gauge. Mirrors p_fatigue for now. */
    @JsonProperty("risk_score")
    private double riskScore;

    /** AU intensities normalised to 0-1 for UI gauges. */
    @JsonProperty("au_intensities")
    private Map<String, Double> auIntensities;

    /** Per-frame latency measured by the vision service itself (ms). */
    @JsonProperty("inference_latency_ms")
    private double inferenceLatencyMs;

    /** Action unit activations detected in this frame. */
    @JsonProperty("active_aus")
    private Map<String, Boolean> activeAus;

    // ---- provenance (relayed verbatim; never defaulted by the gateway) ----

    /**
     * Which vision pipeline produced this row: "stub" (synthetic demo telemetry)
     * or "onnx" (real inference). Null when the vision service did not report it,
     * which the dashboard renders as "unknown" — never as LIVE.
     */
    private String mode;

    /**
     * Canonical provenance, computed by the vision service: "synthetic" or "live".
     *
     * <p>This is the answer to "were these numbers measured or generated?", which
     * is a different question from {@link #mode} ("which code path ran?"). It is
     * carried separately because only the vision service can answer it correctly,
     * and because the narrative layer and the dashboard both need to say it out
     * loud rather than infer it from a mode string.
     *
     * <p>Null when the vision service did not report it; consumers must treat that
     * as "unstated" and never as "live".
     */
    @JsonProperty("mode_class")
    private String modeClass;

    /**
     * Per-metric provenance, e.g. {"class_probs": "synthetic", "ear": "live",
     * "rppg_bpm": "not_configured", "face_detection": "mediapipe"}.
     */
    @JsonProperty("signal_status")
    private Map<String, String> signalStatus;
}
