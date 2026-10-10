package com.fatiguedetect.gateway.service;

import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Observed health of the two downstream services, derived from real traffic.
 *
 * Deliberately NOT a poller: the gateway does not schedule probes, so it never
 * adds load (or a background thread) just to answer a health question. Instead,
 * the pipeline records what it actually saw — the last successful inference, the
 * last failure and its reason, which sessions currently hold a live narrative
 * socket — and {@code /health} reports that.
 *
 * The honest consequence: on a gateway with no active sessions, downstream
 * status is "unknown", not "healthy". A health check must not claim a service is
 * reachable when nothing has tried to reach it.
 */
@Component
public class DownstreamHealth {

    private final Instant startedAt = Instant.now();

    private final AtomicLong visionSuccesses = new AtomicLong();
    private final AtomicLong visionFailures = new AtomicLong();
    private final AtomicLong visionLastSuccessAt = new AtomicLong();
    private final AtomicLong visionLastErrorAt = new AtomicLong();
    private final AtomicReference<String> visionLastErrorReason = new AtomicReference<>();

    /**
     * Monotonic attempt counter, used to order the last success against the last
     * failure.
     *
     * Comparing wall-clock milliseconds is wrong: a failure that lands in the same
     * millisecond as the last success (entirely normal for a fast local call that
     * errors, and always true on a coarse clock) would compare as "not newer" and be
     * reported as healthy. A sequence number has no resolution to lose.
     */
    private final AtomicLong visionAttempts = new AtomicLong();
    private final AtomicLong visionLastSuccessSeq = new AtomicLong();
    private final AtomicLong visionLastFailureSeq = new AtomicLong();

    private final AtomicLong narrativeConnects = new AtomicLong();
    private final AtomicLong narrativeReconnects = new AtomicLong();
    private final AtomicLong narrativeLastErrorAt = new AtomicLong();
    private final AtomicReference<String> narrativeLastErrorReason = new AtomicReference<>();

    /** Sessions whose narrative socket is currently open. */
    private final Set<String> narrativeConnected = ConcurrentHashMap.newKeySet();

    private final AtomicLong framesIngested = new AtomicLong();

    // ---- recording ---------------------------------------------------------

    public void recordFrameIngested() {
        framesIngested.incrementAndGet();
    }

    public void recordVisionSuccess() {
        visionSuccesses.incrementAndGet();
        visionLastSuccessAt.set(System.currentTimeMillis());
        visionLastSuccessSeq.set(visionAttempts.incrementAndGet());
    }

    public void recordVisionFailure(String reason) {
        visionFailures.incrementAndGet();
        visionLastErrorAt.set(System.currentTimeMillis());
        visionLastErrorReason.set(reason);
        visionLastFailureSeq.set(visionAttempts.incrementAndGet());
    }

    public void recordNarrativeConnected(String sessionId) {
        narrativeConnects.incrementAndGet();
        narrativeConnected.add(sessionId);
    }

    public void recordNarrativeDisconnected(String sessionId) {
        narrativeConnected.remove(sessionId);
    }

    public void recordNarrativeReconnect() {
        narrativeReconnects.incrementAndGet();
    }

    public void recordNarrativeError(String reason) {
        narrativeLastErrorAt.set(System.currentTimeMillis());
        narrativeLastErrorReason.set(reason);
    }

    // ---- derived status ----------------------------------------------------

    /**
     * Vision is degraded when the most recent attempt failed and nothing has
     * succeeded since. Before any attempt, the status is unknown rather than
     * degraded — a fresh gateway is not broken.
     */
    public boolean visionDegraded() {
        return visionLastFailureSeq.get() > visionLastSuccessSeq.get();
    }

    /**
     * True once at least one inference attempt has been observed, either way. The
     * distinction matters: "no attempt yet" and "attempts succeeding" are both
     * non-degraded, and reporting both as the same value would make the dashboard
     * unable to tell a healthy pipeline from an untried one.
     */
    public boolean visionObserved() {
        return visionSuccesses.get() + visionFailures.get() > 0;
    }

    /** Narrative is degraded when a session wants a narrative socket and does not have one. */
    public boolean narrativeDegraded(int activeSessions) {
        return narrativeLastErrorAt.get() > 0 && narrativeConnected.size() < activeSessions;
    }

    /** True once a narrative socket has been opened or has failed to open. */
    public boolean narrativeObserved() {
        return narrativeConnects.get() > 0 || narrativeLastErrorAt.get() > 0;
    }

    public String status(int activeSessions) {
        if (visionDegraded() || narrativeDegraded(activeSessions)) {
            return "degraded";
        }
        return "healthy";
    }

    // ---- reporting ---------------------------------------------------------

    public Map<String, Object> snapshot(int activeSessions) {
        Map<String, Object> vision = new LinkedHashMap<>();
        vision.put("succeeded", visionSuccesses.get());
        vision.put("failed", visionFailures.get());
        vision.put("lastSuccessAt", epochOrNull(visionLastSuccessAt.get()));
        vision.put("lastErrorAt", epochOrNull(visionLastErrorAt.get()));
        vision.put("lastErrorReason", visionLastErrorReason.get());
        // Three states, not two: TRUE = failing now, FALSE = observed and healthy,
        // null = nothing has been attempted yet. The UI renders null as "unknown",
        // which is the honest answer for a gateway that has never called out.
        vision.put("degraded", visionObserved() ? Boolean.valueOf(visionDegraded()) : null);

        Map<String, Object> narrative = new LinkedHashMap<>();
        narrative.put("connectedSessions", narrativeConnected.size());
        narrative.put("connects", narrativeConnects.get());
        narrative.put("reconnects", narrativeReconnects.get());
        narrative.put("lastErrorAt", epochOrNull(narrativeLastErrorAt.get()));
        narrative.put("lastErrorReason", narrativeLastErrorReason.get());
        narrative.put(
                "degraded",
                narrativeObserved() ? Boolean.valueOf(narrativeDegraded(activeSessions)) : null);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("vision", vision);
        out.put("narrative", narrative);
        return out;
    }

    public long getFramesIngested() {
        return framesIngested.get();
    }

    public long getUptimeSeconds() {
        return Math.max(0, Instant.now().getEpochSecond() - startedAt.getEpochSecond());
    }

    public Instant getStartedAt() {
        return startedAt;
    }

    private static Object epochOrNull(long millis) {
        return millis <= 0 ? null : millis;
    }
}
