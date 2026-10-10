package com.fatiguedetect.gateway.client;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fatiguedetect.gateway.dto.EnvelopeType;
import com.fatiguedetect.gateway.dto.TelemetryFrame;
import com.fatiguedetect.gateway.dto.WebSocketEnvelope;
import com.fatiguedetect.gateway.service.DownstreamHealth;
import com.fatiguedetect.gateway.service.EnvelopeFactory;
import com.fatiguedetect.gateway.websocket.SessionRegistry;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.socket.WebSocketMessage;
import org.springframework.web.reactive.socket.client.ReactorNettyWebSocketClient;
import reactor.core.Disposable;
import reactor.core.publisher.Mono;
import reactor.core.publisher.Sinks;
import reactor.util.retry.Retry;

import java.io.IOException;
import java.net.URI;
import java.time.Duration;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Per-session WebSocket bridge to the Python narrative XAI service.
 *
 * Why a bridge and not an HTTP client:
 *   The narrative engine is stateful and streaming. It needs a *continuous*
 *   telemetry stream (10 s sliding windows, a ~60 s per-user baseline, level
 *   hysteresis, streamed narrative events). A one-shot POST per high-risk frame
 *   cannot work, and the engine's own NarrationPolicy already decides *when* a
 *   narrative is worth producing — the gateway must not duplicate it.
 *
 * Guarantees:
 *   - {@link #offer} never blocks and never throws: if the send buffer is full
 *     or the socket is reconnecting, the frame is DROPPED (consistent with the
 *     gateway's drop-not-queue policy).
 *   - Telemetry to the browser never depends on this bridge. A dead narrative
 *     service costs narrative events, nothing else.
 *   - Reconnects forever with a fixed delay while the client session is open,
 *     emitting at most one throttled narrative_error per 10 s.
 */
@Slf4j
@Component
public class NarrativeBridge {

    private static final long ERROR_THROTTLE_MS = 10_000L;

    private final ReactorNettyWebSocketClient webSocketClient = new ReactorNettyWebSocketClient();

    private final SessionRegistry sessionRegistry;
    private final EnvelopeFactory envelopeFactory;
    private final ObjectMapper objectMapper;
    private final DownstreamHealth health;
    private final String wsUrl;
    private final int reconnectSeconds;
    private final int sendBuffer;

    private final ConcurrentHashMap<String, Link> links = new ConcurrentHashMap<>();

    public NarrativeBridge(SessionRegistry sessionRegistry,
                           EnvelopeFactory envelopeFactory,
                           ObjectMapper objectMapper,
                           DownstreamHealth health,
                           @Value("${narrative.service.ws-url}") String wsUrl,
                           @Value("${narrative.service.reconnect-seconds:3}") int reconnectSeconds,
                           @Value("${narrative.service.send-buffer:64}") int sendBuffer) {
        this.sessionRegistry = sessionRegistry;
        this.envelopeFactory = envelopeFactory;
        this.objectMapper = objectMapper;
        this.health = health;
        this.wsUrl = wsUrl.endsWith("/") ? wsUrl.substring(0, wsUrl.length() - 1) : wsUrl;
        this.reconnectSeconds = Math.max(1, reconnectSeconds);
        this.sendBuffer = Math.max(1, sendBuffer);
    }

    /** Mutable per-session state: the sink is replaced on every reconnect attempt. */
    private static final class Link {
        private final AtomicReference<Sinks.Many<String>> sink = new AtomicReference<>();
        private final AtomicLong lastErrorAt = new AtomicLong(0L);
        private volatile Disposable subscription;
    }

    /**
     * Opens (or re-opens) the bridge for a session. Safe to call again for a
     * reconnecting client — any previous link is closed first.
     */
    public void open(String sessionId) {
        close(sessionId);

        Link link = new Link();
        links.put(sessionId, link);

        URI uri = URI.create(wsUrl + "/ws/" + sessionId);

        Disposable subscription = webSocketClient.execute(uri, session -> {
                    // Runs only once the socket is actually established, so this is a
                    // truthful "connected" signal rather than an "attempted" one.
                    health.recordNarrativeConnected(sessionId);

                    // A fresh sink per connection attempt: a completed sink cannot be
                    // reused, so it must not outlive the socket it feeds.
                    Sinks.Many<String> sink = Sinks.many().unicast()
                            .onBackpressureBuffer(new ArrayBlockingQueue<>(sendBuffer));
                    link.sink.set(sink);

                    Mono<Void> outbound = session.send(sink.asFlux().map(session::textMessage));
                    Mono<Void> inbound = session.receive()
                            .map(WebSocketMessage::getPayloadAsText)
                            .doOnNext(text -> relay(sessionId, text))
                            .doFinally(signal -> sink.tryEmitComplete())  // unblock the sender when the socket ends
                            .then();

                    // Completing normally means the remote closed: turn it into an error so the
                    // retry loop runs. The type witness is required because the following
                    // doFinally() removes the target type that would otherwise infer Void.
                    return Mono.when(inbound, outbound)
                            .then(Mono.<Void>error(new IOException("narrative websocket closed")))
                            .doFinally(signal -> health.recordNarrativeDisconnected(sessionId));
                })
                .doOnError(e -> log.warn("sessionId={} narrative socket error: {}", sessionId, e.getMessage()))
                .retryWhen(Retry.fixedDelay(Long.MAX_VALUE, Duration.ofSeconds(reconnectSeconds))
                        .doBeforeRetry(rs -> {
                            log.info("sessionId={} narrative service unreachable, retry #{} in {}s",
                                    sessionId, rs.totalRetries() + 1, reconnectSeconds);
                            health.recordNarrativeReconnect();
                            notifyUnavailable(sessionId, link, EnvelopeType.REASON_UNAVAILABLE);
                        }))
                .subscribe(v -> { },
                        e -> log.debug("sessionId={} narrative bridge terminated: {}", sessionId, e.toString()));

        link.subscription = subscription;
        log.info("sessionId={} narrative bridge opened -> {}", sessionId, uri);
    }

    /**
     * Queues one frame for the narrative engine. Never blocks, never throws:
     * a full buffer or a reconnecting socket results in a drop.
     */
    public void offer(String sessionId, TelemetryFrame frame) {
        Link link = links.get(sessionId);
        if (link == null) {
            log.debug("sessionId={} no narrative bridge, frame dropped", sessionId);
            return;
        }
        Sinks.Many<String> sink = link.sink.get();
        if (sink == null) {
            log.debug("sessionId={} narrative bridge not connected yet, frame dropped", sessionId);
            return;
        }
        String json;
        try {
            json = objectMapper.writeValueAsString(frame);
        } catch (Exception e) {
            log.warn("sessionId={} failed to serialise telemetry frame: {}", sessionId, e.getMessage());
            return;
        }
        Sinks.EmitResult result = sink.tryEmitNext(json);
        if (result.isFailure()) {
            log.debug("sessionId={} narrative frame dropped ({})", sessionId, result);
        }
    }

    /** Closes the bridge for a session. Idempotent. */
    public void close(String sessionId) {
        Link link = links.remove(sessionId);
        if (link == null) {
            return;
        }
        Sinks.Many<String> sink = link.sink.getAndSet(null);
        if (sink != null) {
            sink.tryEmitComplete();
        }
        Disposable subscription = link.subscription;
        if (subscription != null && !subscription.isDisposed()) {
            subscription.dispose();
        }
        health.recordNarrativeDisconnected(sessionId);
        log.info("sessionId={} narrative bridge closed", sessionId);
    }

    /** Relays one narrative event to the browser, unchanged apart from the envelope. */
    private void relay(String sessionId, String text) {
        WebSocketEnvelope envelope = envelopeFactory.fromPythonEvent(sessionId, text);
        try {
            sessionRegistry.sendToSession(sessionId, objectMapper.writeValueAsString(envelope));
        } catch (Exception e) {
            log.warn("sessionId={} failed to relay narrative event: {}", sessionId, e.getMessage());
        }
    }

    /** Emits a narrative_error envelope, at most once per {@code ERROR_THROTTLE_MS} per session. */
    private void notifyUnavailable(String sessionId, Link link, String reason) {
        long now = System.currentTimeMillis();
        long last = link.lastErrorAt.get();
        if (now - last < ERROR_THROTTLE_MS || !link.lastErrorAt.compareAndSet(last, now)) {
            return;
        }
        health.recordNarrativeError(reason);
        try {
            sessionRegistry.sendToSession(sessionId,
                    objectMapper.writeValueAsString(WebSocketEnvelope.narrativeError(sessionId, reason)));
        } catch (Exception e) {
            log.debug("sessionId={} failed to send narrative_error: {}", sessionId, e.getMessage());
        }
    }
}
