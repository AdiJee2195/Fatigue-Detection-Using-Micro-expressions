package com.fatiguedetect.gateway;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fatiguedetect.gateway.client.NarrativeBridge;
import com.fatiguedetect.gateway.client.VisionServiceClient;
import com.fatiguedetect.gateway.dto.FrameRequest;
import com.fatiguedetect.gateway.dto.InferenceResult;
import com.fatiguedetect.gateway.dto.WebSocketEnvelope;
import com.fatiguedetect.gateway.websocket.FrameMetrics;
import com.fatiguedetect.gateway.websocket.VideoWebSocketHandler;
import org.junit.jupiter.api.Test;
import org.mockito.Mockito;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.context.annotation.Primary;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.web.reactive.socket.client.ReactorNettyWebSocketClient;
import reactor.core.Disposable;
import reactor.core.publisher.Mono;
import reactor.core.publisher.Sinks;

import java.net.URI;
import java.time.Duration;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assertions.fail;

/**
 * Integration test for the gateway WebSocket pipeline.
 *
 * Fixed relative to the original version, which opened a NEW WebSocket per frame
 * (so "the second frame" went to a different session and backpressure was never
 * exercised) and asserted {@code telemetryCount <= 1}, which passes trivially
 * when zero telemetry is received.
 *
 * Here one connection is used for the whole test, the downstream clients are
 * mocked, and the assertions are exact.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
@Import(GatewayWebSocketIntegrationTest.Stubs.class)
class GatewayWebSocketIntegrationTest {

    private static final String SESSION_ID = "test-operator-001";

    /** Mutable vision behaviour, so each test can script success/failure. */
    private static volatile Function<FrameRequest, Mono<InferenceResult>> visionBehavior =
            request -> Mono.just(fakeResult());

    @TestConfiguration
    static class Stubs {

        @Bean
        @Primary
        VisionServiceClient visionServiceClient() {
            VisionServiceClient mock = Mockito.mock(VisionServiceClient.class);
            Mockito.when(mock.analyzeFrame(Mockito.any(FrameRequest.class)))
                    .thenAnswer(invocation -> visionBehavior.apply(invocation.getArgument(0)));
            return mock;
        }

        @Bean
        @Primary
        NarrativeBridge narrativeBridge() {
            // The bridge dials a real WebSocket; mocked here so the test stays offline.
            return Mockito.mock(NarrativeBridge.class);
        }
    }

    @LocalServerPort
    private int port;

    @Autowired
    private ObjectMapper objectMapper;

    @Autowired
    private VideoWebSocketHandler handler;

    @Autowired
    private org.springframework.boot.test.web.client.TestRestTemplate rest;

    @Test
    void aSecondConnectionCannotTakeOverALiveSession() throws Exception {
        // The end-to-end version of the SessionRegistry rule, over real sockets:
        // a duplicate sessionId must be refused at the transport, and the operator
        // already connected must not be disturbed.
        List<String> received = new CopyOnWriteArrayList<>();
        Sinks.Many<String> outbound = newOutbound();
        Disposable incumbent = openSocket(outbound, received);
        try {
            awaitSessionRegistered();
            // Traffic, so the incumbent is unambiguously "live" and not stale.
            outbound.tryEmitNext(frameJson(1_700_000_000_000L));
            Thread.sleep(200);

            List<String> intruderReceived = new CopyOnWriteArrayList<>();
            Disposable intruder = openSocket(newOutbound(), intruderReceived);
            try {
                // The refused socket must be closed by the server, which completes
                // the client-side Mono. A bounded wait keeps a regression a failure
                // rather than a hang.
                long deadline = System.currentTimeMillis() + 5_000;
                while (!intruder.isDisposed() && System.currentTimeMillis() < deadline) {
                    Thread.sleep(25);
                }
                assertTrue(intruder.isDisposed(), "the duplicate connection must be closed");
                assertTrue(intruderReceived.isEmpty(),
                        "a refused connection must receive no telemetry: " + intruderReceived);
            } finally {
                intruder.dispose();
            }

            // The incumbent is still alive and still owns its session.
            assertFalse(incumbent.isDisposed(), "the live session must not be evicted");
            assertTrue(handler.metrics(SESSION_ID).isPresent(),
                    "the live session must still be registered");
            outbound.tryEmitNext(frameJson(1_700_000_000_100L));
            Thread.sleep(250);
            assertTrue(handler.metrics(SESSION_ID).orElseThrow().getFramesReceived() >= 2,
                    "the incumbent must keep being served");
        } finally {
            incumbent.dispose();
        }
    }

    @Test
    void dropsSecondFrameWhileTheFirstIsStillInFlight() throws Exception {
        visionBehavior = request -> Mono.just(fakeResult()).delayElement(Duration.ofMillis(400));

        List<String> received = new CopyOnWriteArrayList<>();
        Sinks.Many<String> outbound = newOutbound();
        Disposable connection = openSocket(outbound, received);
        try {
            awaitSessionRegistered();

            outbound.tryEmitNext(frameJson(1_700_000_000_000L));
            Thread.sleep(120);   // first vision call is still running
            outbound.tryEmitNext(frameJson(1_700_000_000_100L));
            Thread.sleep(120);

            FrameMetrics metrics = handler.metrics(SESSION_ID).orElseThrow();
            assertEquals(2, metrics.getFramesReceived(), "both frames arrived");
            assertEquals(1, metrics.getFramesDropped(), "the second frame must be dropped, not queued");
            assertEquals(0, metrics.getFramesProcessed(), "nothing has finished yet");
            assertEquals(0, countType(received, "telemetry"), "no telemetry while the first frame is in flight");

            Thread.sleep(500);   // first call completes

            assertEquals(1, metrics.getFramesProcessed());
            assertEquals(1, countType(received, "telemetry"), "the first frame produces exactly one telemetry envelope");
            assertEquals(1, metrics.getFramesDropped(), "the drop count must not keep growing");

            // The in-flight flag was released, so a later frame is processed normally.
            outbound.tryEmitNext(frameJson(1_700_000_000_200L));
            Thread.sleep(500);
            assertEquals(3, metrics.getFramesReceived());
            assertEquals(2, metrics.getFramesProcessed());
            assertEquals(2, countType(received, "telemetry"));

            // The browser cannot see dropped frames (they produce no message), so the
            // diagnostics endpoint must agree with the in-process counters.
            Map<String, Object>[] rows = sessionsFromMonitoring();
            assertEquals(1, rows.length, "exactly one live session");
            assertEquals(SESSION_ID, rows[0].get("sessionId"));
            assertEquals(3L, ((Number) rows[0].get("framesReceived")).longValue());
            assertEquals(2L, ((Number) rows[0].get("framesProcessed")).longValue());
            assertEquals(1L, ((Number) rows[0].get("framesDropped")).longValue());
        } finally {
            connection.dispose();
        }
    }

    @Test
    void visionFailureIsReportedAndTheSocketRecovers() throws Exception {
        AtomicInteger calls = new AtomicInteger();
        visionBehavior = request -> calls.incrementAndGet() == 1
                ? Mono.<InferenceResult>error(new TimeoutException("vision service timed out"))
                : Mono.just(fakeResult());

        List<String> received = new CopyOnWriteArrayList<>();
        Sinks.Many<String> outbound = newOutbound();
        Disposable connection = openSocket(outbound, received);
        try {
            awaitSessionRegistered();

            outbound.tryEmitNext(frameJson(1_700_000_100_000L));
            Thread.sleep(300);

            assertEquals(1, countType(received, "telemetry_error"), "a failed frame must produce telemetry_error");
            assertEquals(0, countType(received, "telemetry"));
            assertEquals("vision_service_timeout", reasonOf(received, "telemetry_error"),
                    "a timeout must be classified, not reported generically");

            FrameMetrics metrics = handler.metrics(SESSION_ID).orElseThrow();
            assertEquals(1, metrics.getFramesReceived());
            assertEquals(0, metrics.getFramesProcessed(), "a failed frame is not a processed frame");
            assertEquals(0, metrics.getFramesDropped(), "a vision failure is not a backpressure drop");

            // The socket stayed open and the pipeline resumes by itself.
            outbound.tryEmitNext(frameJson(1_700_000_100_100L));
            Thread.sleep(400);
            assertEquals(1, countType(received, "telemetry"), "processing resumes once the vision service answers");
            assertEquals(1, metrics.getFramesProcessed());
        } finally {
            connection.dispose();
        }
    }

    @Test
    void malformedFrameIsIgnoredWithoutKillingTheSession() throws Exception {
        visionBehavior = request -> Mono.just(fakeResult());

        List<String> received = new CopyOnWriteArrayList<>();
        Sinks.Many<String> outbound = newOutbound();
        Disposable connection = openSocket(outbound, received);
        try {
            awaitSessionRegistered();

            outbound.tryEmitNext("{this is not valid json");
            Thread.sleep(200);
            assertTrue(received.isEmpty(), "a malformed frame must not produce an envelope");

            FrameMetrics metrics = handler.metrics(SESSION_ID).orElseThrow();
            // The frame arrived, so it counts as received — but it was never
            // dispatched to the vision service, so it is neither processed nor
            // dropped. received - processed - dropped therefore counts unparseable frames.
            assertEquals(1, metrics.getFramesReceived());
            assertEquals(0, metrics.getFramesProcessed());
            assertEquals(0, metrics.getFramesDropped());

            outbound.tryEmitNext(frameJson(1_700_000_200_000L));
            Thread.sleep(300);
            assertEquals(1, countType(received, "telemetry"), "the session is still usable afterwards");
        } finally {
            connection.dispose();
        }
    }

    /**
     * Regression guard for a real bug found by running the end-to-end smoke test:
     * with Tomcat's default 8 KB text buffer, a perfectly ordinary ~30 KB browser
     * frame was closed with 1009 before the handler ever saw it, so the gateway could
     * not accept realistic frames at all.
     */
    @Test
    void acceptsABrowserSizedFrameWellAboveTomcatsDefaultBuffer() throws Exception {
        visionBehavior = request -> Mono.just(fakeResult());

        List<String> received = new CopyOnWriteArrayList<>();
        Sinks.Many<String> outbound = newOutbound();
        Disposable connection = openSocket(outbound, received);
        try {
            awaitSessionRegistered();

            // ~300 KB of base64 in the envelope: typical of a 640px JPEG at quality
            // 0.6, and 37x larger than Tomcat's default buffer.
            outbound.tryEmitNext(frameJson(1_700_000_500_000L, 300_000));
            Thread.sleep(600);

            assertEquals(1, countType(received, "telemetry"),
                    "a realistic frame must be processed, not rejected by the container");
            assertNull(firstOfType(received, "telemetry_error"),
                    "no 1009-style failure may surface as a telemetry error");
        } finally {
            connection.dispose();
        }
    }

    @Test
    void rejectsAnOversizedFrameWithoutKillingTheSession() throws Exception {
        visionBehavior = request -> Mono.just(fakeResult());
        int maxFrameBytes = 524288; // gateway.max-frame-bytes

        List<String> received = new CopyOnWriteArrayList<>();
        Sinks.Many<String> outbound = newOutbound();
        Disposable connection = openSocket(outbound, received);
        try {
            awaitSessionRegistered();

            // Above the handler's limit but inside the container's headroom: the frame
            // must be dropped on its own, and the session must survive.
            outbound.tryEmitNext(frameJson(1_700_000_600_000L, maxFrameBytes + 1024));
            Thread.sleep(400);
            assertEquals(0, countType(received, "telemetry"));

            outbound.tryEmitNext(frameJson(1_700_000_600_100L));
            Thread.sleep(400);
            assertEquals(1, countType(received, "telemetry"),
                    "the session must keep working after an oversized frame");

            FrameMetrics metrics = handler.metrics(SESSION_ID).orElseThrow();
            // An oversized frame is rejected before it is counted, so the one valid
            // frame is the only thing the metrics may show. This is the documented
            // semantics: received - processed - dropped counts *unparseable* frames,
            // deliberately excluding payloads that never passed the size gate.
            assertEquals(1, metrics.getFramesReceived(),
                    "the oversized frame must not be counted as received");
            assertEquals(1, metrics.getFramesProcessed());
            assertEquals(0, metrics.getFramesDropped(), "rejection is not a backpressure drop");
        } finally {
            connection.dispose();
        }
    }

    @Test
    void relaysProvenanceSoTheBrowserCanTellSyntheticFromLive() throws Exception {
        // Provenance is the only thing that stops the dashboard presenting generated
        // numbers as measurements, so the gateway must relay it verbatim.
        visionBehavior = request -> Mono.just(fakeResult());

        List<String> received = new CopyOnWriteArrayList<>();
        Sinks.Many<String> outbound = newOutbound();
        Disposable connection = openSocket(outbound, received);
        try {
            awaitSessionRegistered();

            outbound.tryEmitNext(frameJson(1_700_000_300_000L));
            Thread.sleep(400);

            JsonNode payload = payloadOf(received, "telemetry");
            assertEquals("stub", payload.get("mode").asText(),
                    "the pipeline mode must reach the browser unchanged");
            assertEquals("synthetic", payload.get("signal_status").get("class_probs").asText());
            assertEquals("synthetic", payload.get("signal_status").get("ear").asText());
            assertEquals("not_configured", payload.get("signal_status").get("rppg_bpm").asText());
        } finally {
            connection.dispose();
        }
    }

    @Test
    void absentProvenanceStaysAbsentInsteadOfBeingDefaultedToLive() throws Exception {
        // A vision service that does not report provenance must not be upgraded by the
        // gateway into an implied claim about the data.
        visionBehavior = request -> Mono.just(resultWithoutProvenance());

        List<String> received = new CopyOnWriteArrayList<>();
        Sinks.Many<String> outbound = newOutbound();
        Disposable connection = openSocket(outbound, received);
        try {
            awaitSessionRegistered();

            outbound.tryEmitNext(frameJson(1_700_000_400_000L));
            Thread.sleep(400);

            JsonNode payload = payloadOf(received, "telemetry");
            JsonNode mode = payload.get("mode");
            assertTrue(mode == null || mode.isNull(),
                    "an unreported mode must stay null, so the UI can say 'unknown'");
            JsonNode status = payload.get("signal_status");
            assertTrue(status == null || status.isNull(),
                    "unreported provenance must stay null, never be defaulted");
        } finally {
            connection.dispose();
        }
    }

    // ---- helpers -------------------------------------------------------------

    private JsonNode payloadOf(List<String> received, String type) throws Exception {
        String json = received.stream()
                .filter(candidate -> type.equals(typeOf(candidate)))
                .findFirst()
                .orElseThrow(() -> new IllegalStateException("no " + type + " envelope was received"));
        return objectMapper.readTree(json).get("payload");
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object>[] sessionsFromMonitoring() {
        Map<String, Object>[] rows = rest.getForObject("/sessions", Map[].class);
        assertNotNull(rows, "/sessions must return a JSON array");
        return rows;
    }

    /** Bounded, warm-up tolerant sink: emits before the handler subscribes are buffered. */
    private static Sinks.Many<String> newOutbound() {
        return Sinks.many().unicast().onBackpressureBuffer(new ArrayBlockingQueue<>(64));
    }

    private Disposable openSocket(Sinks.Many<String> outbound, List<String> received) {
        URI uri = URI.create("ws://localhost:" + port + "/ws/video/" + SESSION_ID);
        return new ReactorNettyWebSocketClient()
                .execute(uri, session -> Mono.when(
                        session.send(outbound.asFlux().map(session::textMessage)),
                        session.receive().doOnNext(m -> received.add(m.getPayloadAsText())).then()
                ).then())
                .subscribe();
    }

    private void awaitSessionRegistered() throws InterruptedException {
        long deadline = System.currentTimeMillis() + 5_000;
        while (System.currentTimeMillis() < deadline) {
            if (handler.metrics(SESSION_ID).isPresent()) {
                return;
            }
            Thread.sleep(25);
        }
        fail("session was not registered within 5s");
    }

    private String frameJson(long timestampMs) throws Exception {
        return frameJson(timestampMs, 20);
    }

    /**
     * Builds a frame request whose base64 payload is exactly {@code frameChars}
     * characters long, so the boundary tests can target the gateway's byte ceilings
     * precisely. 'A' is in the base64 alphabet, and the payload is only ever measured
     * here — the mocked vision client never decodes it.
     */
    private String frameJson(long timestampMs, int frameChars) throws Exception {
        FrameRequest request = new FrameRequest();
        request.setFrame("A".repeat(Math.max(1, frameChars)));
        request.setClientTimestamp(timestampMs);
        return objectMapper.writeValueAsString(request);
    }

    private String firstOfType(List<String> received, String type) {
        return received.stream().filter(json -> type.equals(typeOf(json))).findFirst().orElse(null);
    }

    private int countType(List<String> received, String type) {
        return (int) received.stream().filter(json -> type.equals(typeOf(json))).count();
    }

    private String typeOf(String json) {
        try {
            return objectMapper.readValue(json, WebSocketEnvelope.class).getType();
        } catch (Exception e) {
            return null;
        }
    }

    private String reasonOf(List<String> received, String type) throws Exception {
        String json = received.stream().filter(t -> type.equals(typeOf(t))).findFirst().orElseThrow();
        return objectMapper.readTree(json).get("payload").get("reason").asText();
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
        // A demo-mode response: the dashboard must be able to say so.
        result.setMode("stub");
        result.setSignalStatus(Map.of(
                "class_probs", "synthetic",
                "ear", "synthetic",
                "au", "synthetic",
                "rppg_bpm", "not_configured",
                "face_detection", "synthetic"));
        return result;
    }

    /** Simulates an older vision service that predates the provenance fields. */
    private static InferenceResult resultWithoutProvenance() {
        InferenceResult result = fakeResult();
        result.setMode(null);
        result.setSignalStatus(null);
        return result;
    }
}
