package com.fatiguedetect.gateway.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fatiguedetect.gateway.client.NarrativeBridge;
import com.fatiguedetect.gateway.client.VisionServiceClient;
import com.fatiguedetect.gateway.dto.FrameRequest;
import com.fatiguedetect.gateway.dto.InferenceResult;
import com.fatiguedetect.gateway.dto.TelemetryFrame;
import com.fatiguedetect.gateway.dto.WebSocketEnvelope;
import com.fatiguedetect.gateway.websocket.FrameMetrics;
import com.fatiguedetect.gateway.websocket.SessionRegistry;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.mockito.Mockito;
import reactor.core.publisher.Mono;

import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Unit tests for the frame pipeline glue: timestamp normalisation, the telemetry
 * envelope, the hand-off to the narrative bridge, and in-flight flag release.
 */
class OrchestrationServiceTest {

    private static final String SESSION = "op-1";
    private static final String OTHER_SESSION = "op-2";

    private VisionServiceClient visionClient;
    private NarrativeBridge narrativeBridge;
    private SessionRegistry sessionRegistry;
    private DownstreamHealth health;
    private OrchestrationService service;

    @BeforeEach
    void setUp() {
        visionClient = Mockito.mock(VisionServiceClient.class);
        narrativeBridge = Mockito.mock(NarrativeBridge.class);
        sessionRegistry = Mockito.mock(SessionRegistry.class);
        health = new DownstreamHealth();
        Mockito.when(visionClient.analyzeFrame(Mockito.any(FrameRequest.class)))
                .thenReturn(Mono.just(fakeResult()));
        service = new OrchestrationService(visionClient, narrativeBridge, sessionRegistry,
                new ObjectMapper(), health);
    }

    // ---- timestamp contract --------------------------------------------------

    @Test
    void convertsClientMillisToEpochSecondsAndNeverGoesBackwards() {
        assertEquals(1_700_000_000.0, service.nextTimestamp(SESSION, 1_700_000_000_000L), 1e-9);
        assertEquals(1_700_000_003.0, service.nextTimestamp(SESSION, 1_700_000_003_000L), 1e-9);

        double duplicate = service.nextTimestamp(SESSION, 1_700_000_003_000L);
        assertTrue(duplicate > 1_700_000_003.0, "a repeated client timestamp must still increase");

        double backwards = service.nextTimestamp(SESSION, 1_000L);
        assertTrue(backwards > duplicate, "an out-of-order client clock must not move time backwards");

        double absentClock = service.nextTimestamp(SESSION, 0L);
        assertTrue(absentClock > backwards, "a missing client clock must fall back to strictly-increasing time");
    }

    @Test
    void timestampsAreSecondsNotMilliseconds() {
        double ts = service.nextTimestamp(SESSION, 1_700_000_000_000L);
        assertTrue(ts < 1e10, "the narrative engine expects epoch SECONDS; ms values would break its windows");
    }

    @Test
    void sessionsKeepIndependentClocks() {
        service.nextTimestamp(SESSION, 1_700_000_100_000L);
        assertEquals(1_700_000_000.0, service.nextTimestamp(OTHER_SESSION, 1_700_000_000_000L), 1e-9);
    }

    @Test
    void clearSessionResetsPerSessionState() {
        service.nextTimestamp(SESSION, 1_700_000_100_000L);
        service.clearSession(SESSION);
        // A fresh session may accept an earlier timestamp again.
        assertEquals(1_700_000_000.0, service.nextTimestamp(SESSION, 1_700_000_000_000L), 1e-9);
    }

    // ---- pipeline behaviour --------------------------------------------------

    @Test
    void pushesTelemetryAndOffersTheFrameToTheNarrativeBridge() throws Exception {
        AtomicBoolean inFlight = new AtomicBoolean(false);
        FrameMetrics metrics = new FrameMetrics();

        service.processFrame(SESSION, frame(1_700_000_000_000L), System.currentTimeMillis(), inFlight, metrics);

        assertFalse(inFlight.get(), "doFinally must always release the in-flight flag");

        ArgumentCaptor<String> envelopes = ArgumentCaptor.forClass(String.class);
        Mockito.verify(sessionRegistry).sendToSession(Mockito.eq(SESSION), envelopes.capture());
        JsonNode sent = new ObjectMapper().readTree(envelopes.getValue());
        assertEquals("telemetry", sent.get("type").asText());
        assertEquals(SESSION, sent.get("sessionId").asText());
        assertEquals(0.10, sent.get("payload").get("risk_score").asDouble(), 1e-9);
        assertTrue(sent.get("timestamp").asLong() > 0, "the envelope carries a gateway timestamp");

        ArgumentCaptor<TelemetryFrame> frames = ArgumentCaptor.forClass(TelemetryFrame.class);
        Mockito.verify(narrativeBridge).offer(Mockito.eq(SESSION), frames.capture());
        TelemetryFrame frame = frames.getValue();
        assertEquals(1_700_000_000.0, frame.getTs(), 1e-9);
        assertEquals(1, frame.getFrameId());
        assertEquals(0.05, frame.getClassProbs().get("microsleep"), 1e-9);

        assertEquals(1, metrics.getFramesProcessed());
        assertEquals(0, metrics.getFramesDropped());
    }

    @Test
    void frameIdsIncreasePerSession() {
        service.processFrame(SESSION, frame(1_700_000_000_000L), System.currentTimeMillis(), new AtomicBoolean(), null);
        service.processFrame(SESSION, frame(1_700_000_000_100L), System.currentTimeMillis(), new AtomicBoolean(), null);
        service.processFrame(OTHER_SESSION, frame(1_700_000_000_200L), System.currentTimeMillis(), new AtomicBoolean(), null);

        ArgumentCaptor<TelemetryFrame> frames = ArgumentCaptor.forClass(TelemetryFrame.class);
        Mockito.verify(narrativeBridge, Mockito.times(3)).offer(Mockito.anyString(), frames.capture());
        List<TelemetryFrame> all = frames.getAllValues();
        assertEquals(1, all.get(0).getFrameId());
        assertEquals(2, all.get(1).getFrameId());
        assertEquals(1, all.get(2).getFrameId(), "a different session restarts the counter");
        assertTrue(all.get(1).getTs() > all.get(0).getTs(), "timestamps must increase across frames");
    }

    @Test
    void visionFailureYieldsTelemetryErrorAndOffersNothingDownstream() throws Exception {
        Mockito.when(visionClient.analyzeFrame(Mockito.any(FrameRequest.class)))
                .thenReturn(Mono.error(new IllegalStateException("connection refused")));
        AtomicBoolean inFlight = new AtomicBoolean(false);
        FrameMetrics metrics = new FrameMetrics();

        service.processFrame(SESSION, frame(1_700_000_000_000L), System.currentTimeMillis(), inFlight, metrics);

        assertFalse(inFlight.get(), "the flag must be released on the error path too");

        ArgumentCaptor<String> envelopes = ArgumentCaptor.forClass(String.class);
        Mockito.verify(sessionRegistry).sendToSession(Mockito.eq(SESSION), envelopes.capture());
        JsonNode sent = new ObjectMapper().readTree(envelopes.getValue());
        assertEquals("telemetry_error", sent.get("type").asText());
        assertEquals("vision_service_error", sent.get("payload").get("reason").asText());

        Mockito.verify(narrativeBridge, Mockito.never()).offer(Mockito.anyString(), Mockito.any());
        assertEquals(0, metrics.getFramesProcessed());
        assertEquals(0, metrics.getFramesDropped(), "a failed inference is not a dropped frame");
    }

    @Test
    void visionHttpErrorIsClassifiedDistinctly() throws Exception {
        Mockito.when(visionClient.analyzeFrame(Mockito.any(FrameRequest.class)))
                .thenReturn(Mono.error(new org.springframework.web.reactive.function.client
                        .WebClientResponseException(500, "Server Error", null, null, null)));

        service.processFrame(SESSION, frame(1_700_000_000_000L), System.currentTimeMillis(), new AtomicBoolean(), null);

        ArgumentCaptor<String> envelopes = ArgumentCaptor.forClass(String.class);
        Mockito.verify(sessionRegistry).sendToSession(Mockito.eq(SESSION), envelopes.capture());
        JsonNode sent = new ObjectMapper().readTree(envelopes.getValue());
        assertEquals("vision_service_http_error", sent.get("payload").get("reason").asText());
    }

    @Test
    void visionOutcomesAreRecordedForHealthReporting() {
        service.processFrame(SESSION, frame(1_700_000_000_000L), System.currentTimeMillis(),
                new AtomicBoolean(), null);
        assertTrue(health.getFramesIngested() > 0, "every dispatched frame is counted as ingested");
        assertFalse(health.visionDegraded(), "a successful inference must clear the degraded state");

        Mockito.when(visionClient.analyzeFrame(Mockito.any(FrameRequest.class)))
                .thenReturn(Mono.<InferenceResult>error(new java.util.concurrent.TimeoutException("slow")));
        service.processFrame(SESSION, frame(1_700_000_001_000L), System.currentTimeMillis(),
                new AtomicBoolean(), null);

        assertTrue(health.visionDegraded(), "a failure with no later success means degraded");
        assertEquals("vision_service_timeout", health.snapshot(1).get("vision") instanceof java.util.Map<?, ?> v
                ? v.get("lastErrorReason") : null);
    }

    @Test
    void telemetrySurvivesAnUnavailableNarrativeService() throws Exception {
        // Even a bridge that throws on every offer must not corrupt the frame path.
        Mockito.doThrow(new IllegalStateException("bridge down"))
                .when(narrativeBridge).offer(Mockito.anyString(), Mockito.any(TelemetryFrame.class));
        FrameMetrics metrics = new FrameMetrics();

        service.processFrame(SESSION, frame(1_700_000_000_000L), System.currentTimeMillis(), new AtomicBoolean(), metrics);

        ArgumentCaptor<String> envelopes = ArgumentCaptor.forClass(String.class);
        Mockito.verify(sessionRegistry, Mockito.times(1)).sendToSession(Mockito.eq(SESSION), envelopes.capture());
        JsonNode sent = new ObjectMapper().readTree(envelopes.getValue());
        assertEquals("telemetry", sent.get("type").asText(),
                "a narrative failure must not turn a good frame into a telemetry_error");
        assertEquals(1, metrics.getFramesProcessed());
    }

    // ---- fixtures ------------------------------------------------------------

    private static FrameRequest frame(long timestampMs) {
        FrameRequest request = new FrameRequest();
        request.setFrame("ZmFrZQ==");
        request.setClientTimestamp(timestampMs);
        return request;
    }

    private static InferenceResult fakeResult() {
        InferenceResult result = new InferenceResult();
        result.setClassProbs(Map.of("microsleep", 0.05, "neutral", 0.90, "yawning", 0.05));
        result.setPFatigue(0.10);
        result.setRiskScore(0.10);
        result.setInferenceLatencyMs(30.0);
        result.setEar(0.27);
        result.setAu(Map.of("AU4", 1.0));
        result.setAuIntensities(Map.of("AU4", 0.2));
        result.setActiveAus(Map.of("AU4", false));
        result.setFaceDetected(true);
        return result;
    }
}
