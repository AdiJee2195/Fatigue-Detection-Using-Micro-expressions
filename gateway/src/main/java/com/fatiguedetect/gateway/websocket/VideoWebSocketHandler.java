package com.fatiguedetect.gateway.websocket;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fatiguedetect.gateway.client.NarrativeBridge;
import com.fatiguedetect.gateway.dto.FrameRequest;
import com.fatiguedetect.gateway.service.OrchestrationService;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;
import reactor.core.Disposable;
import reactor.core.publisher.Flux;

import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * WebSocket handler for incoming video frames.
 *
 * Why TextWebSocketHandler (not Binary)?
 * Frames arrive as JSON-wrapped base64 strings: { "frame": "<b64>", "timestamp": 123 }.
 * Using TextWebSocketHandler lets us use Jackson for deserialization directly.
 * If we switch to raw binary frames later, we swap this for BinaryWebSocketHandler
 * and decode bytes ourselves — the rest of the pipeline (backpressure, orchestration)
 * stays identical.
 *
 * Backpressure: if the vision service is still processing a frame when a new one
 * arrives, the new frame is dropped (never queued) so the client is always
 * looking at the freshest data.
 */
@Slf4j
@Component
public class VideoWebSocketHandler extends TextWebSocketHandler {

    /**
     * 1008 POLICY_VIOLATION with an explicit reason, used when a second connection
     * claims a sessionId that is still live. A dedicated status keeps this
     * distinguishable from an ordinary protocol error, so the browser can say
     * "this session is already open elsewhere" instead of looping on reconnects.
     */
    static final CloseStatus DUPLICATE_SESSION_STATUS =
            new CloseStatus(1008, "session_already_active");

    private final SessionRegistry sessionRegistry;
    private final OrchestrationService orchestrationService;
    private final NarrativeBridge narrativeBridge;
    private final ObjectMapper objectMapper;
    private final int warnAfterDrops;
    private final int maxFrameBytes;
    private final long metricsIntervalSeconds;

    /**
     * Per-session in-flight flag: true = vision call in progress, new frames must
     * be dropped. This is the backpressure mechanism.
     */
    private final ConcurrentHashMap<String, AtomicBoolean> inFlight = new ConcurrentHashMap<>();

    /** Per-session consecutive drop counter — lets us escalate from DEBUG to WARN. */
    private final ConcurrentHashMap<String, AtomicInteger> consecutiveDrops = new ConcurrentHashMap<>();

    /** Per-session metrics — lifecycle tied to session open/close. */
    private final ConcurrentHashMap<String, FrameMetrics> metricsMap = new ConcurrentHashMap<>();

    /** In-flight vision subscriptions, cancelled when the client disconnects. */
    private final ConcurrentHashMap<String, Disposable> inFlightCalls = new ConcurrentHashMap<>();

    /** Periodic metrics log tickers, disposed on close (no @Scheduled, no pooling). */
    private final ConcurrentHashMap<String, Disposable> metricsTickers = new ConcurrentHashMap<>();

    /**
     * Time source for admission decisions. Injected so a test can advance past the
     * stale-session grace period instantly instead of sleeping for it — a sleep
     * long enough to be reliable would make the suite slow and still flaky.
     */
    private final java.util.function.LongSupplier clock;

    private static volatile long fakeNowMs = 1_700_000_000_000L;

    public VideoWebSocketHandler(SessionRegistry sessionRegistry,
                                 OrchestrationService orchestrationService,
                                 NarrativeBridge narrativeBridge,
                                 ObjectMapper objectMapper,
                                 @Value("${gateway.backpressure.warn-after-drops:50}") int warnAfterDrops,
                                 @Value("${gateway.max-frame-bytes:524288}") int maxFrameBytes,
                                 @Value("${gateway.metrics-interval-seconds:5}") long metricsIntervalSeconds,
                                 @Value("${gateway.clock:system}") String clock) {
        // "fake" lets an integration test drive admission deterministically
        // without sleeping for the stale-session grace period.
        this.clock = "fake".equalsIgnoreCase(clock) ? () -> fakeNowMs : System::currentTimeMillis;
        this.sessionRegistry = sessionRegistry;
        this.orchestrationService = orchestrationService;
        this.narrativeBridge = narrativeBridge;
        this.objectMapper = objectMapper;
        this.warnAfterDrops = warnAfterDrops;
        this.maxFrameBytes = maxFrameBytes;
        this.metricsIntervalSeconds = metricsIntervalSeconds;
    }

    @Override
    public void afterConnectionEstablished(WebSocketSession session) {
        String sessionId = extractSessionId(session);
        if (sessionId == null) {
            log.error("No sessionId in URI, closing session ws_id={}", session.getId());
            try { session.close(CloseStatus.NOT_ACCEPTABLE); } catch (Exception ignored) { }
            return;
        }
        // Admission control. A duplicate id or a full gateway is refused *here*,
        // before any per-session state exists, so a rejected connection cannot
        // leave a half-registered session, a narrative socket or a metrics ticker
        // behind. See SessionRegistry for why a duplicate is rejected rather than
        // taken over.
        SessionRegistry.Admission admission = sessionRegistry.register(sessionId, session, clock());
        if (admission == SessionRegistry.Admission.REJECTED_DUPLICATE) {
            closeQuietly(session, DUPLICATE_SESSION_STATUS,
                    "session id is already in use by a live connection");
            return;
        }
        if (admission == SessionRegistry.Admission.REJECTED_CAPACITY) {
            closeQuietly(session, CloseStatus.SERVICE_OVERLOAD,
                    "gateway session capacity reached");
            return;
        }
        inFlight.put(sessionId, new AtomicBoolean(false));
        consecutiveDrops.put(sessionId, new AtomicInteger(0));
        metricsMap.put(sessionId, new FrameMetrics());

        // Open the (long-lived, stateful) narrative connection for this session.
        narrativeBridge.open(sessionId);
        startMetricsTicker(sessionId);

        log.info("sessionId={} connection established", sessionId);
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        String sessionId = extractSessionId(session);
        if (sessionId == null) {
            return;
        }
        // Only the connection that owns this sessionId may tear it down.
        //
        // A refused duplicate connection also reaches this callback (with the same
        // sessionId from the URL), so without this guard the very act of rejecting
        // an intruder would deregister the live operator: their narrative socket
        // closed and their metrics lost. That is the takeover this admission
        // control exists to prevent, arriving through the cleanup path instead.
        if (!sessionRegistry.isOwner(sessionId, session)) {
            log.info("sessionId={} ws_id={} closed but is not the registered owner; "
                            + "leaving the live session untouched",
                    sessionId, session.getId());
            return;
        }
        // Cancel work that was still in flight for this session.
        dispose(inFlightCalls.remove(sessionId));
        dispose(metricsTickers.remove(sessionId));

        narrativeBridge.close(sessionId);
        orchestrationService.clearSession(sessionId);
        sessionRegistry.remove(sessionId);
        inFlight.remove(sessionId);
        consecutiveDrops.remove(sessionId);

        FrameMetrics metrics = metricsMap.remove(sessionId);
        if (metrics != null) {
            log.info("SESSION_CLOSED {}", metrics.toSummaryString(sessionId));
        }
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) {
        String sessionId = extractSessionId(session);
        if (sessionId == null) {
            return;
        }

        // Any inbound message counts as activity: it is what distinguishes a live
        // session from a half-open one left behind by a dropped network, and so it
        // is what decides whether a takeover attempt is refused.
        sessionRegistry.touch(sessionId, clock());

        String payload = message.getPayload();
        if (payload == null || payload.isEmpty()) {
            log.warn("sessionId={} empty frame payload", sessionId);
            return;
        }
        // Base64 payloads exceed Tomcat's 8 KB default text buffer; oversized input is
        // rejected here rather than buffered. Chars == bytes for base64 (ASCII).
        if (payload.length() > maxFrameBytes) {
            log.warn("sessionId={} frame rejected: {} chars exceeds max_frame_bytes={}",
                    sessionId, payload.length(), maxFrameBytes);
            return;
        }

        FrameMetrics metrics = metricsMap.get(sessionId);
        if (metrics != null) {
            metrics.recordReceived();
        }

        FrameRequest frame;
        try {
            frame = objectMapper.readValue(payload, FrameRequest.class);
        } catch (Exception e) {
            log.warn("sessionId={} invalid frame payload: {}", sessionId, e.getMessage());
            return;
        }

        // --- Backpressure gate ---
        // If the previous frame is still being processed by the vision service, drop
        // this one immediately. We never queue: a queued frame would be stale by the
        // time it was processed — the client will send fresher data anyway.
        AtomicBoolean inFlightFlag = inFlight.computeIfAbsent(sessionId, k -> new AtomicBoolean(false));
        if (!inFlightFlag.compareAndSet(false, true)) {
            int drops = consecutiveDrops.computeIfAbsent(sessionId, k -> new AtomicInteger(0))
                    .incrementAndGet();
            if (metrics != null) {
                metrics.recordDropped();
            }
            if (drops <= warnAfterDrops) {
                log.debug("sessionId={} frame_ts={} dropped=true consecutive_drops={}",
                        sessionId, frame.getClientTimestamp(), drops);
            } else if (drops == warnAfterDrops + 1) {
                log.warn("sessionId={} frame dropped count exceeded {}, suppressing DEBUG logs",
                        sessionId, warnAfterDrops);
            }
            return;
        }

        // Reset the consecutive drop counter since we're processing this frame.
        consecutiveDrops.computeIfAbsent(sessionId, k -> new AtomicInteger(0)).set(0);
        long ingestTimestamp = System.currentTimeMillis();
        log.debug("sessionId={} frame_ts={} dropped=false ingest_ts={}",
                sessionId, frame.getClientTimestamp(), ingestTimestamp);

        // Hand off to orchestration — non-blocking; the Mono callback clears the
        // in-flight flag when complete (success or error). The subscription is kept
        // so a disconnecting client does not leave a vision call running.
        Disposable call = orchestrationService.processFrame(
                sessionId, frame, ingestTimestamp, inFlightFlag, metrics);
        dispose(inFlightCalls.put(sessionId, call));
    }

    /**
     * Per-session metrics, for the periodic log line and for tests/metrics endpoints.
     * Empty once the session has closed.
     */
    public Optional<FrameMetrics> metrics(String sessionId) {
        return Optional.ofNullable(metricsMap.get(sessionId));
    }

    /**
     * Immutable snapshot of live per-session metrics, for the {@code /sessions}
     * monitoring endpoint. The map is a copy; the {@link FrameMetrics} instances are
     * the live ones and it is safe to read their counters concurrently.
     */
    public Map<String, FrameMetrics> metricsSnapshot() {
        return Map.copyOf(metricsMap);
    }

    private void startMetricsTicker(String sessionId) {
        if (metricsIntervalSeconds <= 0) {
            return;
        }
        Disposable ticker = Flux.interval(Duration.ofSeconds(metricsIntervalSeconds))
                .subscribe(tick -> {
                    FrameMetrics m = metricsMap.get(sessionId);
                    if (m != null && m.getFramesReceived() > 0) {
                        log.info("METRICS {}", m.toSummaryString(sessionId));
                    }
                }, e -> log.debug("sessionId={} metrics ticker stopped: {}", sessionId, e.getMessage()));
        metricsTickers.put(sessionId, ticker);
    }

    private long clock() {
        return clock.getAsLong();
    }

    /** Test hook: set the time used when {@code gateway.clock=fake}. */
    static void setFakeClock(long nowMs) {
        fakeNowMs = nowMs;
    }

    /**
     * Refuse a connection without letting the close itself fail the handler.
     * A rejected socket is a normal outcome, not an error worth a stack trace.
     */
    private void closeQuietly(WebSocketSession session, CloseStatus status, String reason) {
        log.info("closing ws_id={} with {}({}): {}", session.getId(), status.getCode(),
                status.getReason(), reason);
        try {
            session.close(status);
        } catch (Exception e) {
            log.debug("error closing rejected session ws_id={}: {}", session.getId(), e.getMessage());
        }
    }

    private static void dispose(Disposable disposable) {
        if (disposable != null && !disposable.isDisposed()) {
            disposable.dispose();
        }
    }

    /**
     * Extracts the sessionId from the WebSocket URI path.
     * URI format: /ws/video/{sessionId}
     * Path segments: ["", "ws", "video", "{sessionId}"]
     *
     * The path segment is percent-decoded, because {@code URI.getPath()} keeps the
     * raw escaped form: a session id such as {@code operator 12} or {@code a/b}
     * arrives as {@code operator%2012} and would otherwise be used verbatim as a
     * map key — producing a session that the engine and the browser disagree about.
     * Decoding failures fall back to the raw segment rather than dropping the
     * connection.
     */
    private String extractSessionId(WebSocketSession session) {
        URI uri = session.getUri();
        if (uri == null) {
            return null;
        }
        String path = uri.getPath();
        String prefix = "/ws/video/";
        if (path == null || !path.startsWith(prefix) || path.length() <= prefix.length()) {
            return null;
        }
        String raw = path.substring(prefix.length());
        if (raw.indexOf('%') < 0) {
            return raw;
        }
        try {
            return URLDecoder.decode(raw, StandardCharsets.UTF_8);
        } catch (IllegalArgumentException e) {
            log.warn("malformed percent-encoding in session id, using the raw segment: {}", raw);
            return raw;
        }
    }
}
