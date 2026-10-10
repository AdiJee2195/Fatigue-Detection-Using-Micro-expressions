package com.fatiguedetect.gateway.dto;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;

/**
 * The provenance hop from the vision service to the narrative engine.
 *
 * <p>This is a regression test for a real defect: {@code TelemetryFrame.from} used
 * to copy only the measurement fields, so {@code mode} and {@code signal_status}
 * were silently dropped at this boundary. The dashboard knew a reading was
 * synthetic while the narrative engine — and the language model it briefs — did
 * not, which is precisely how generated numbers get written up as observations of
 * a driver. Every number was correct; only the sentence was false.
 *
 * <p>The assertions are deliberately about forwarding, not about meaning: the
 * gateway must relay what the producer said and must never invent a value.
 */
class TelemetryFrameProvenanceTest {

    private final ObjectMapper mapper = new ObjectMapper();

    private static InferenceResult result(String mode, String modeClass,
                                          Map<String, String> signalStatus) {
        InferenceResult r = new InferenceResult();
        r.setTs(1700000000.0);
        r.setClassProbs(Map.of("microsleep", 0.3, "neutral", 0.5, "yawning", 0.2));
        r.setPFatigue(0.5);
        r.setMode(mode);
        r.setModeClass(modeClass);
        r.setSignalStatus(signalStatus);
        return r;
    }

    @Test
    void provenanceIsForwardedToTheNarrativeContract() {
        Map<String, String> status = Map.of("class_probs", "synthetic", "ear", "synthetic");
        TelemetryFrame frame = TelemetryFrame.from(result("stub", "synthetic", status), 7L);

        assertEquals("stub", frame.getMode());
        assertEquals("synthetic", frame.getModeClass());
        assertEquals(status, frame.getSignalStatus());
        assertEquals(7L, frame.getFrameId());
    }

    @Test
    void liveProvenanceIsForwardedUnchanged() {
        TelemetryFrame frame = TelemetryFrame.from(
                result("onnx", "live", Map.of("ear", "live", "rppg_bpm", "not_configured")), 1L);

        assertEquals("onnx", frame.getMode());
        assertEquals("live", frame.getModeClass());
        assertEquals("not_configured", frame.getSignalStatus().get("rppg_bpm"));
    }

    @Test
    void anAbsentProvenanceStaysAbsentRatherThanBeingDefaulted() {
        // The critical property: silence must not become a claim. If the gateway
        // defaulted this to "live", every consumer downstream would state something
        // the producer never said.
        TelemetryFrame frame = TelemetryFrame.from(result(null, null, null), 1L);

        assertNull(frame.getMode());
        assertNull(frame.getModeClass());
        assertNull(frame.getSignalStatus());
    }

    @Test
    void theHeadersComeFromTheVisionServiceNotFromTheGateway() {
        // The gateway copies; it does not compute a mode class from a mode string.
        // That derivation belongs to the component that actually ran the pipeline.
        TelemetryFrame frame = TelemetryFrame.from(result("stub", "live", Map.of()), 1L);

        assertEquals("live", frame.getModeClass(),
                "the gateway must not re-derive provenance from `mode`");
    }

    @Test
    void theWireNamesMatchThePythonSchema() {
        // Field names are a cross-language contract with
        // xai_narrative/fatigue_xai/schemas.py :: FrameTelemetry.
        String json = json(result("stub", "synthetic", Map.of("ear", "synthetic")));

        assertEquals("stub", jsonOf(json).get("mode"));
        assertEquals("synthetic", jsonOf(json).get("mode_class"));
        assertFalse(json.contains("modeClass"), "snake_case only: " + json);
        assertSame(Boolean.TRUE, jsonOf(json).get("face_detected"),
                "an unspecified face_detected defaults to true for back-compat");
    }

    private String json(InferenceResult r) {
        try {
            return mapper.writeValueAsString(TelemetryFrame.from(r, 1L));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> jsonOf(String json) {
        try {
            return mapper.readValue(json, Map.class);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
