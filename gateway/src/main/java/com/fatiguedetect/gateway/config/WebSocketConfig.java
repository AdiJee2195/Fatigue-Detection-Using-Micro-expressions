package com.fatiguedetect.gateway.config;

import com.fatiguedetect.gateway.websocket.VideoWebSocketHandler;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.socket.config.annotation.EnableWebSocket;
import org.springframework.web.socket.config.annotation.WebSocketConfigurer;
import org.springframework.web.socket.config.annotation.WebSocketHandlerRegistry;
import org.springframework.web.socket.server.standard.ServletServerContainerFactoryBean;

/**
 * Registers the video WebSocket endpoint.
 *
 * The session id is part of the URL (/ws/video/{sessionId}) and is parsed from the
 * session URI by {@link VideoWebSocketHandler}; no handshake interceptor is used.
 *
 * <h2>Two frame ceilings, on purpose</h2>
 *
 * Frames arrive as base64 inside a JSON envelope, so a single message can be large.
 * There are two independent limits and they behave differently:
 *
 * <ol>
 *   <li><b>The handler limit</b> ({@code gateway.max-frame-bytes}, default 512 KB).
 *       A frame above it is rejected by {@link VideoWebSocketHandler} and the session
 *       carries on — the next frame is processed normally.</li>
 *   <li><b>The container ceiling</b>: the handler limit plus
 *       {@link #CONTAINER_HEADROOM_BYTES}. A message this large is never delivered to
 *       the handler at all: Tomcat closes the connection with 1009 ("message too
 *       big"). This is the backstop that stops a hostile or broken client from
 *       driving an unbounded allocation.</li>
 * </ol>
 *
 * The headroom exists so that the <em>graceful</em> path is the one that runs for
 * ordinary client mistakes. With Tomcat's 8 KB default, or with a headroom of only a
 * few KB, overshooting the documented limit by a little would tear down the session
 * and force a reconnect — losing the narrative engine's in-progress narrative over a
 * single oversized frame. The cost of the headroom is one buffer per open session.
 */
@Configuration
@EnableWebSocket
public class WebSocketConfig implements WebSocketConfigurer {

    /**
     * Extra bytes allowed above the handler limit before the container kills the
     * connection. Sized so that a frame comfortably over the documented limit is
     * rejected gracefully instead of dropping the session.
     */
    static final int CONTAINER_HEADROOM_BYTES = 64 * 1024;

    /** Slack for the JSON envelope ({@code {"frame":"…","timestamp":1234567890123}}). */
    private static final int ENVELOPE_OVERHEAD_BYTES = 128;

    private final VideoWebSocketHandler videoWebSocketHandler;
    private final String[] allowedOrigins;
    private final int maxTextMessageBufferSize;

    public WebSocketConfig(VideoWebSocketHandler videoWebSocketHandler,
                           @Value("${gateway.allowed-origins:http://localhost:5173,http://localhost:3000}")
                           String allowedOrigins,
                           @Value("${gateway.max-frame-bytes:524288}") int maxFrameBytes) {
        this.videoWebSocketHandler = videoWebSocketHandler;
        this.allowedOrigins = allowedOrigins.split("\\s*,\\s*");
        this.maxTextMessageBufferSize = maxFrameBytes + ENVELOPE_OVERHEAD_BYTES + CONTAINER_HEADROOM_BYTES;
    }

    @Override
    public void registerWebSocketHandlers(WebSocketHandlerRegistry registry) {
        registry.addHandler(videoWebSocketHandler, "/ws/video/{sessionId}")
                .setAllowedOrigins(allowedOrigins);
    }

    /**
     * Tomcat's default text message buffer is 8 KB, but a base64 JPEG is far larger —
     * without this the container closes the socket with status 1009 ("message too
     * big") before the handler ever sees a frame, which would make a browser sending
     * ordinary 640px frames unusable.
     *
     * The idle timeout is deliberately explicit: an active session sends 15 frames a
     * second, so any I/O resets it, and only a genuinely abandoned connection (crashed
     * tab, lost network) reaches it. Reclaiming that session is the point; the client
     * reconnects on its own and the narrative engine keeps the operator's baseline.
     */
    @Bean
    public ServletServerContainerFactoryBean createWebSocketContainer(
            @Value("${gateway.session-idle-timeout-ms:900000}") long sessionIdleTimeoutMs) {
        ServletServerContainerFactoryBean container = new ServletServerContainerFactoryBean();
        container.setMaxTextMessageBufferSize(maxTextMessageBufferSize);
        container.setMaxBinaryMessageBufferSize(maxTextMessageBufferSize);
        container.setMaxSessionIdleTimeout(sessionIdleTimeoutMs);
        return container;
    }
}
