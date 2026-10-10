package com.fatiguedetect.gateway.dto;

import org.junit.jupiter.api.Test;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Optional;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The event vocabulary is a contract with the Python engine, so it is asserted
 * explicitly: any accidental rename breaks the pipeline silently otherwise.
 */
class NarrativeEventTypeTest {

    @Test
    void wireNamesAreTheFrozenContractWithTheNarrativeEngine() {
        assertEquals("assessment", NarrativeEventType.ASSESSMENT.wireName());
        assertEquals("narrative_start", NarrativeEventType.NARRATIVE_START.wireName());
        assertEquals("narrative_chunk", NarrativeEventType.NARRATIVE_CHUNK.wireName());
        assertEquals("narrative_retry", NarrativeEventType.NARRATIVE_RETRY.wireName());
        assertEquals("narrative_final", NarrativeEventType.NARRATIVE_FINAL.wireName());
        assertEquals("narrative_cancelled", NarrativeEventType.NARRATIVE_CANCELLED.wireName());
        assertEquals("error", NarrativeEventType.ERROR.wireName());
    }

    @Test
    void onlyTheEngineErrorIsRenamedOnTheWayOut() {
        // "error" would be ambiguous for the browser; everything else passes through.
        for (NarrativeEventType type : NarrativeEventType.values()) {
            if (type == NarrativeEventType.ERROR) {
                assertEquals(EnvelopeType.NARRATIVE_ERROR, type.envelopeType());
            } else {
                assertEquals(type.wireName(), type.envelopeType());
            }
        }
    }

    @Test
    void wireRoundTripsAndIsCaseInsensitive() {
        for (NarrativeEventType type : NarrativeEventType.values()) {
            assertSame(type, NarrativeEventType.fromWire(type.wireName()).orElseThrow());
            assertSame(type, NarrativeEventType.fromWire(type.wireName().toUpperCase()).orElseThrow());
            assertSame(type, NarrativeEventType.fromWire("  " + type.wireName() + "  ").orElseThrow());
        }
    }

    @Test
    void unknownAndMissingNamesAreRejected() {
        assertFalse(NarrativeEventType.fromWire("telemetry_v2").isPresent());
        assertFalse(NarrativeEventType.fromWire("ASSESSMENT_UPDATE").isPresent());
        assertFalse(NarrativeEventType.fromWire(null).isPresent());
        assertFalse(NarrativeEventType.fromWire("").isPresent());
        assertFalse(NarrativeEventType.fromWire("   ").isPresent());
        assertEquals(Optional.empty(), NarrativeEventType.fromWire("narrative_chunks"));
    }

    @Test
    void wireNamesAreUniqueAndVersioned() {
        Set<String> seen = new HashSet<>();
        for (NarrativeEventType type : NarrativeEventType.values()) {
            assertTrue(seen.add(type.wireName()), "duplicate wire name: " + type.wireName());
        }
        assertEquals(NarrativeEventType.values().length, seen.size());
        assertEquals(1, NarrativeEventType.PROTOCOL_VERSION,
                "bump PROTOCOL_VERSION only together with producers and consumers");
        // Guard against a silent rename of the provisional-text event.
        assertTrue(Arrays.stream(NarrativeEventType.values())
                .anyMatch(t -> t.wireName().equals("narrative_chunk")));
    }
}
