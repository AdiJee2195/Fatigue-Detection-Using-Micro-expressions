package com.fatiguedetect.gateway.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fatiguedetect.gateway.client.NarrativeBridge;
import com.fatiguedetect.gateway.client.VisionServiceClient;
import com.fatiguedetect.gateway.dto.EnvelopeType;
import com.fatiguedetect.gateway.dto.FrameRequest;
import com.fatiguedetect.gateway.dto.TelemetryFrame;
import com.fatiguedetect.gateway.dto.WebSocketEnvelope;
import com.fatiguedetect.gateway.websocket.FrameMetrics;
import com.fatiguedetect.gateway.websocket.SessionRegistry;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import reactor.core.Disposable;

import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Orchestrates the frame pipeline:
 *   1. Forward the frame to the vision service (non-blocking WebFlux call)
 *   2. Push the telemetry envelope back to the client
 *   3. Offer the frame's telemetry to the per-session narrative bridge
 *
 * This class never blocks — every downstream call is a Mono subscription, and
 * the narrative bridge's offer() is a non-blocking, drop-on-full enqueue.
 *
 * Note what is NOT here any more: narrative scheduling. Deciding *when* to
 * narrate requires session state (sliding windows, per-user baseline, level
 * hysteresis) that only the Python engine has, so the policy lives there. The
 * gateway relays every processed frame and lets the engine decide.
 */
@Slf4j
@Service
public class OrchestrationService {

    /** Sub-millisecond nudge used to keep timestamps strictly increasing within a session. */
    private static final double TS_EPSILON = 0.001;

    private final VisionServiceClient visionClient;
    private final NarrativeBridge narrativeBridge;
    private final SessionRegistry sessionRegistry;
    private final ObjectMapper objectMapper;
    private final DownstreamHealth health;

    /** Last epoch-seconds timestamp sent downstream, per session. */
    private final ConcurrentHashMap<String, Double> lastTs = new ConcurrentHashMap<>();

    /** Monotonic per-session frame counter. */
    private final ConcurrentHashMap<String, AtomicLong> frameCounters = new ConcurrentHashMap<>();

    public OrchestrationService(VisionServiceClient visionClient,
                                NarrativeBridge narrativeBridge,
                                SessionRegistry sessionRegistry,
                                ObjectMapper objectMapper,
                                DownstreamHealth health) {
        this.visionClient = visionClient;
        this.narrativeBridge = narrativeBridge;
        this.sessionRegistry = sessionRegistry;
        this.objectMapper = objectMapper;
        this.health = health;
    }

    /**
     * Processes a single frame. Returns immediately with the in-flight
     * subscription so the caller can cancel it if the client disconnects.
     *
     * @param sessionId        operator/device session identifier
     * @param frame            parsed frame request from the client
     * @param ingestTimestamp  gateway-side timestamp when the frame was received (ms)
     * @param inFlightFlag     atomic flag cleared when the call settles (success or error)
     * @param metrics          per-session metrics tracker (nullable)
     */
    public Disposable processFrame(String sessionId, FrameRequest frame,
                                   long ingestTimestamp, AtomicBoolean inFlightFlag,
                                   FrameMetrics metrics) {
        frame.setSessionId(sessionId);
        health.recordFrameIngested();

        return visionClient.analyzeFrame(frame)
                .doOnSuccess(result -> {
                    long latencyMs = System.currentTimeMillis() - ingestTimestamp;
                    log.info("sessionId={} frame_ts={} latency_ms={} dropped=false",
                            sessionId, frame.getClientTimestamp(), latencyMs);
                    health.recordVisionSuccess();

                    if (metrics != null) {
                        metrics.recordProcessed(latencyMs);
                    }
                    if (result == null) {
                        // Empty body (e.g. 204): nothing to forward.
                        return;
                    }

                    // 1. Telemetry straight back to the client.
                    sendEnvelope(sessionId, WebSocketEnvelope.telemetry(sessionId, result));

                    // 2. Same frame offered to the narrative engine, with a
                    //    strictly-increasing epoch-seconds timestamp.
                    //    NOTE: the vision service's own `ts` field is ADVISORY only — the
                    //    gateway always overwrites it here, because only the gateway can
                    //    guarantee monotonicity per session (client clocks are untrusted).
                    result.setTs(nextTimestamp(sessionId, frame.getClientTimestamp()));
                    long frameId = frameCounters
                            .computeIfAbsent(sessionId, k -> new AtomicLong())
                            .incrementAndGet();
                    // Defensive: a narrative-layer problem must never turn a good
                    // frame into a telemetry_error or break the frame path.
                    try {
                        narrativeBridge.offer(sessionId, TelemetryFrame.from(result, frameId));
                    } catch (Exception e) {
                        log.warn("sessionId={} frame_ts={} narrative hand-off failed: {}",
                                sessionId, frame.getClientTimestamp(), e.getMessage());
                    }
                })
                .doOnError(e -> {
                    long latencyMs = System.currentTimeMillis() - ingestTimestamp;
                    log.warn("sessionId={} frame_ts={} latency_ms={} error={}",
                            sessionId, frame.getClientTimestamp(), latencyMs, e.getMessage());
                    String reason = classifyError(e);
                    health.recordVisionFailure(reason);
                    // Never let the client hang on a failed frame.
                    sendEnvelope(sessionId, WebSocketEnvelope.telemetryError(sessionId, reason));
                })
                .doFinally(signal -> inFlightFlag.set(false))
                .subscribe(v -> { }, e -> { /* already handled in doOnError */ });
    }

    /**
     * Converts the client's epoch-millisecond timestamp to epoch seconds and
     * guarantees the result is strictly greater than the previous value for
     * this session.
     *
     * The narrative engine uses ts for windowing, blink rates and trend slope;
     * a non-increasing timestamp makes it drop frames or mis-measure intervals.
     * Client clocks are untrusted, so any out-of-order, zero or non-finite value
     * is clamped to (previous + epsilon) or to the gateway wall clock.
     *
     * Package-private so it can be unit tested directly.
     */
    double nextTimestamp(String sessionId, long clientTimestampMs) {
        double candidate = clientTimestampMs / 1000.0;
        if (!Double.isFinite(candidate) || candidate <= 0.0) {
            candidate = System.currentTimeMillis() / 1000.0;
        }
        return lastTs.merge(sessionId, candidate,
                (previous, next) -> next > previous ? next : previous + TS_EPSILON);
    }

    /** Drops per-session state. Called when the client disconnects. */
    public void clearSession(String sessionId) {
        lastTs.remove(sessionId);
        frameCounters.remove(sessionId);
    }

    private void sendEnvelope(String sessionId, WebSocketEnvelope envelope) {
        try {
            sessionRegistry.sendToSession(sessionId, objectMapper.writeValueAsString(envelope));
        } catch (JsonProcessingException e) {
            log.error("sessionId={} failed to serialize envelope: {}", sessionId, e.getMessage());
        }
    }

    /** Maps reactive/HTTP errors to a stable string the client can display. */
    private String classifyError(Throwable e) {
        String name = e.getClass().getSimpleName();
        if (name.contains("TimeoutException")) return EnvelopeType.REASON_VISION_TIMEOUT;
        if (name.contains("WebClientResponseException")) return EnvelopeType.REASON_VISION_HTTP_ERROR;
        return EnvelopeType.REASON_VISION_ERROR;
    }
}
