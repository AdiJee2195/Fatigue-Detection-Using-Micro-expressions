package com.fatiguedetect.gateway.service;

import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

/**
 * Downstream health is reported from observed traffic only — the gateway never
 * probes. That makes the *absence* of data meaningful, and these tests pin the
 * three-state contract the dashboard depends on:
 *
 * <pre>
 *   degraded = TRUE   the most recent attempt failed and nothing has succeeded since
 *   degraded = FALSE  traffic has been observed and is currently succeeding
 *   degraded = null   nothing has been attempted yet — unknown, NOT healthy
 * </pre>
 *
 * Collapsing the last two into a single value is the bug this guards against: the
 * dashboard then shows "unknown" next to "1703 succeeded / 0 failed", which is
 * exactly the sort of contradiction that makes an operator stop trusting the page.
 */
class DownstreamHealthTest {

    @SuppressWarnings("unchecked")
    private static Map<String, Object> vision(DownstreamHealth health, int activeSessions) {
        return (Map<String, Object>) health.snapshot(activeSessions).get("vision");
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> narrative(DownstreamHealth health, int activeSessions) {
        return (Map<String, Object>) health.snapshot(activeSessions).get("narrative");
    }

    @Test
    void anIdleGatewayReportsUnknownNotHealthy() {
        DownstreamHealth health = new DownstreamHealth();

        assertNull(vision(health, 0).get("degraded"),
                "nothing has been attempted, so health must be unknown rather than a claim");
        assertNull(narrative(health, 0).get("degraded"));

        // The overall status is still healthy: an idle gateway is not broken.
        assertEquals("healthy", health.status(0));
    }

    @Test
    void observedSuccessIsReportedAsAnExplicitFalse() {
        DownstreamHealth health = new DownstreamHealth();
        health.recordVisionSuccess();

        assertEquals(Boolean.FALSE, vision(health, 0).get("degraded"),
                "traffic was observed and is succeeding, which the UI must be able to say");
        assertEquals(1L, ((Number) vision(health, 0).get("succeeded")).longValue());
        assertEquals("healthy", health.status(0));
    }

    @Test
    void aFailureAfterTheLastSuccessIsDegraded() {
        DownstreamHealth health = new DownstreamHealth();
        health.recordVisionSuccess();
        health.recordVisionFailure("vision_service_timeout");

        Map<String, Object> vision = vision(health, 0);
        assertEquals(Boolean.TRUE, vision.get("degraded"));
        assertEquals("vision_service_timeout", vision.get("lastErrorReason"));
        assertEquals("degraded", health.status(0));
    }

    @Test
    void aSuccessAfterTheLastFailureClearsTheDegradation() {
        DownstreamHealth health = new DownstreamHealth();
        health.recordVisionFailure("vision_service_error");
        assertEquals("degraded", health.status(0));

        health.recordVisionSuccess();

        assertEquals(Boolean.FALSE, vision(health, 0).get("degraded"),
                "one good frame is enough to leave the degraded state");
        assertEquals("healthy", health.status(0));
    }

    @Test
    void narrativeIsDegradedOnlyWhileASessionIsMissingItsSocket() {
        DownstreamHealth health = new DownstreamHealth();

        health.recordNarrativeError("narrative_service_unavailable");
        assertEquals(Boolean.TRUE, narrative(health, 1).get("degraded"),
                "one active session and no narrative socket is a degradation");

        // The bridge reconnects and the same session gets its socket back.
        health.recordNarrativeConnected("op-1");
        assertEquals(Boolean.FALSE, narrative(health, 1).get("degraded"));
        assertEquals("healthy", health.status(1));

        // A session closing must not leave the count stuck above the active sessions.
        health.recordNarrativeDisconnected("op-1");
        assertEquals(0, ((Number) narrative(health, 1).get("connectedSessions")).intValue());
    }

    @Test
    void framesAreCountedPerIngestNotPerSuccess() {
        DownstreamHealth health = new DownstreamHealth();
        health.recordFrameIngested();
        health.recordFrameIngested();
        health.recordFrameIngested();

        assertEquals(3L, health.getFramesIngested(),
                "frames ingested counts attempts, so a failing vision service still shows traffic");
        assertNull(vision(health, 0).get("degraded"), "ingest alone does not probe anything");
    }
}
