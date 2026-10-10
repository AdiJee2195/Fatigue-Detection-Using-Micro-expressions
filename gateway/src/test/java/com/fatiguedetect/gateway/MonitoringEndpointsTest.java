package com.fatiguedetect.gateway;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.context.annotation.Import;
import org.springframework.http.ResponseEntity;
import org.springframework.test.context.ActiveProfiles;

import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The monitoring endpoints the dashboard depends on.
 *
 * Uses the same Spring context as {@link GatewayWebSocketIntegrationTest} (same
 * profile and the same mocked downstreams) so it does not pay for a second
 * application start-up.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
@Import(GatewayWebSocketIntegrationTest.Stubs.class)
class MonitoringEndpointsTest {

    @Autowired
    private TestRestTemplate rest;

    @Test
    void healthReportsServiceIdentityAndDownstreamShape() {
        ResponseEntity<Map<String, Object>> response =
                rest.exchange("/health", org.springframework.http.HttpMethod.GET, null,
                        new org.springframework.core.ParameterizedTypeReference<>() {
                        });

        assertEquals(200, response.getStatusCode().value());
        Map<String, Object> body = response.getBody();
        assertNotNull(body);

        // The gateway is never "unhealthy" while it can answer: it has no DB or broker
        // that could be down, and it serves telemetry when both downstreams are gone.
        assertTrue(Set.of("healthy", "degraded").contains(body.get("status")),
                "unexpected status: " + body.get("status"));
        assertEquals("fatigue-detect-gateway", body.get("service"));
        assertEquals("0.1.0", body.get("version"));
        assertTrue(((Number) body.get("sessions")).intValue() >= 0);
        assertTrue(((Number) body.get("uptimeSeconds")).longValue() >= 0);

        @SuppressWarnings("unchecked")
        Map<String, Object> downstream = (Map<String, Object>) body.get("downstream");
        assertNotNull(downstream, "downstream block is required");
        assertEquals("http://localhost:9999", downstream.get("visionUrl"));
        assertEquals("ws://localhost:9998", downstream.get("narrativeUrl"));

        @SuppressWarnings("unchecked")
        Map<String, Object> vision = (Map<String, Object>) downstream.get("vision");
        // No inference has been attempted in this test, so health must report
        // "nothing observed" (null) rather than claiming the service is fine.
        assertNull(vision.get("lastSuccessAt"), "an unprobed downstream must not report a success");
        assertNull(vision.get("degraded"), "an unprobed downstream must not report degraded");
    }

    @Test
    void sessionsEndpointReturnsAJsonArray() {
        ResponseEntity<List<Map<String, Object>>> response =
                rest.exchange("/sessions", org.springframework.http.HttpMethod.GET, null,
                        new org.springframework.core.ParameterizedTypeReference<>() {
                        });
        assertEquals(200, response.getStatusCode().value());
        assertNotNull(response.getBody());
    }
}
