package com.fatiguedetect.gateway.web;

import com.fatiguedetect.gateway.service.DownstreamHealth;
import com.fatiguedetect.gateway.websocket.FrameMetrics;
import com.fatiguedetect.gateway.websocket.SessionRegistry;
import com.fatiguedetect.gateway.websocket.VideoWebSocketHandler;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Read-only monitoring endpoints for the dashboard and for orchestrators.
 *
 * Why these exist: the browser cannot observe backpressure. Dropped frames
 * produce no message by design (that is the whole point of drop-don't-queue), so
 * without a diagnostics endpoint the UI could never show that frames are being
 * dropped or how effective the pipeline is. These endpoints expose that state.
 *
 * Both handlers read in-memory structures only — no I/O, no blocking, no polling.
 * Downstream reachability is reported from observed traffic, never probed.
 */
@RestController
public class MonitoringController {

    private static final String VERSION = "0.1.0";

    private final DownstreamHealth health;
    private final SessionRegistry sessionRegistry;
    private final VideoWebSocketHandler handler;
    private final String serviceName;
    private final String visionUrl;
    private final String narrativeUrl;

    public MonitoringController(DownstreamHealth health,
                                SessionRegistry sessionRegistry,
                                VideoWebSocketHandler handler,
                                @Value("${spring.application.name:fatigue-detect-gateway}") String serviceName,
                                @Value("${vision.service.url}") String visionUrl,
                                @Value("${narrative.service.ws-url}") String narrativeUrl) {
        this.health = health;
        this.sessionRegistry = sessionRegistry;
        this.handler = handler;
        this.serviceName = serviceName;
        this.visionUrl = visionUrl;
        this.narrativeUrl = narrativeUrl;
    }

    /**
     * Liveness plus observed downstream health.
     *
     * {@code status} is {@code healthy} or {@code degraded}. The gateway itself is
     * never {@code unhealthy} while it can answer: it holds no connection to a
     * database or broker that could be down, and it is designed to keep serving
     * telemetry when both downstream services are unavailable.
     */
    @GetMapping("/health")
    public Map<String, Object> health() {
        int activeSessions = sessionRegistry.activeSessionIds().size();

        Map<String, Object> downstream = health.snapshot(activeSessions);
        downstream.put("visionUrl", visionUrl);
        downstream.put("narrativeUrl", narrativeUrl);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("status", health.status(activeSessions));
        out.put("service", serviceName);
        out.put("version", VERSION);
        out.put("uptimeSeconds", health.getUptimeSeconds());
        out.put("sessions", activeSessions);
        // Capacity is reported next to the count so "are we near the limit?" is
        // answerable from the dashboard. A rejected connection is otherwise only
        // visible in the gateway log, on a machine the operator may not have open.
        out.put("sessionCapacity", sessionRegistry.capacity());
        out.put("sessionsAvailable", Math.max(0, sessionRegistry.capacity() - activeSessions));
        out.put("framesIngested", health.getFramesIngested());
        out.put("downstream", downstream);
        return out;
    }

    /**
     * Per-session pipeline metrics — how fast frames are flowing and how many are
     * being dropped to keep them fresh.
     */
    @GetMapping("/sessions")
    public List<Map<String, Object>> sessions() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (String sessionId : sessionRegistry.activeSessionIds()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("sessionId", sessionId);
            FrameMetrics metrics = handler.metrics(sessionId).orElse(null);
            if (metrics == null) {
                row.put("framesReceived", 0L);
                row.put("framesProcessed", 0L);
                row.put("framesDropped", 0L);
                row.put("avgLatencyMs", -1.0);
                row.put("p95LatencyMs", -1.0);
                row.put("effectiveFps", -1.0);
            } else {
                row.put("framesReceived", metrics.getFramesReceived());
                row.put("framesProcessed", metrics.getFramesProcessed());
                row.put("framesDropped", metrics.getFramesDropped());
                row.put("avgLatencyMs", metrics.getAvgLatencyMs());
                row.put("p95LatencyMs", metrics.getP95LatencyMs());
                row.put("effectiveFps", metrics.getEffectiveFps());
            }
            out.add(row);
        }
        return out;
    }
}
