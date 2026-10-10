package com.fatiguedetect.gateway.dto;

import org.junit.jupiter.api.Test;

import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The narrative error vocabulary is a three-language contract, and it silently
 * drifted once already.
 *
 * <p>The gateway emitted {@code narrative_event_missing_type} and
 * {@code unknown_narrative_event_type} while the dashboard's
 * {@code NarrativeErrorReason} union declared {@code missing_type} and
 * {@code unknown_type}. Every real reason missed the declared type and fell
 * through to a generic message. Nothing crashed, which is why it survived review:
 * an unmatched reason degrades into a vaguer error rather than a visible one.
 *
 * <p>These assertions pin the Java values. The Python set lives in
 * {@code xai_narrative/fatigue_xai/errors.py} and has its own test; the TypeScript
 * union is checked by {@code frontend/tests/contract.test.ts}. A rename on any
 * side now fails that side's build instead of quietly degrading the dashboard.
 */
class NarrativeErrorReasonsTest {

    /** The canonical set, written out literally on purpose — this is the contract. */
    private static final Set<String> CANONICAL = Set.of(
            "invalid_telemetry_frame",
            "missing_type",
            "unknown_type",
            "narrative_service_unavailable",
            "malformed_narrative_event"
    );

    @Test
    void theGatewayOnlyEverEmitsCanonicalReasons() {
        Set<String> emitted = Set.of(
                EnvelopeType.REASON_INVALID_TELEMETRY_FRAME,
                EnvelopeType.REASON_MISSING_TYPE,
                EnvelopeType.REASON_UNKNOWN_TYPE,
                EnvelopeType.REASON_UNAVAILABLE,
                EnvelopeType.REASON_MALFORMED_EVENT
        );
        assertEquals(CANONICAL, emitted);
    }

    @Test
    void everyReasonIsASnakeCaseCodeNotProse() {
        // The original defect in one assertion: "invalid telemetry frame" is a
        // sentence, and a sentence can never match a union of identifiers.
        for (String reason : CANONICAL) {
            assertTrue(reason.matches("[a-z][a-z0-9_]*"),
                    reason + " is not a canonical code");
            assertFalse(reason.contains(" "), reason + " contains whitespace");
        }
    }

    @Test
    void theOldMismatchedNamesAreGone() {
        // Regression guards for the exact strings that used to be emitted.
        Set<String> emitted = Set.of(
                EnvelopeType.REASON_MISSING_TYPE,
                EnvelopeType.REASON_UNKNOWN_TYPE
        );
        assertFalse(emitted.contains("narrative_event_missing_type"));
        assertFalse(emitted.contains("unknown_narrative_event_type"));
    }

    @Test
    void reasonsAreDistinctAndNonEmpty() {
        for (String reason : CANONICAL) {
            assertFalse(reason.isEmpty());
        }
        assertEquals(CANONICAL.size(), 5, "a duplicate would mean two names for one condition");
    }
}
