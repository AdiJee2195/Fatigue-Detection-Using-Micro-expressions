package com.fatiguedetect.gateway.websocket;

import org.junit.jupiter.api.Test;
import org.springframework.web.socket.WebSocketSession;

import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Session admission: the rules that stop one operator's session being taken over,
 * and stop the gateway being filled up by connections that never send a frame.
 *
 * <p>Times are passed in explicitly. Sleeping for a real grace period would make
 * these tests slow and, worse, flaky — the exact condition being tested
 * (\"nothing has arrived for five seconds\") is not something a sleep can assert
 * reliably.
 */
class SessionAdmissionTest {

    private static final long T0 = 1_700_000_000_000L;
    private static final long GRACE_MS = 5_000L;

    private final AtomicInteger ids = new AtomicInteger();

    private WebSocketSession open() {
        WebSocketSession s = mock(WebSocketSession.class);
        when(s.getId()).thenReturn("ws-" + ids.incrementAndGet());
        when(s.isOpen()).thenReturn(true);
        return s;
    }

    private SessionRegistry registry(int maxSessions) {
        return new SessionRegistry(maxSessions, GRACE_MS);
    }

    // -- the takeover rule -------------------------------------------------

    @Test
    void aSecondConnectionCannotEvictALiveSession() {
        SessionRegistry reg = registry(32);
        WebSocketSession first = open();

        assertEquals(SessionRegistry.Admission.ACCEPTED, reg.register("op-1", first, T0));
        assertEquals(SessionRegistry.Admission.REJECTED_DUPLICATE,
                reg.register("op-1", open(), T0 + 100));

        assertEquals(first, reg.get("op-1"), "the incumbent must still own the session");
        assertTrue(first.isOpen(), "the incumbent must not have been closed");
    }

    @Test
    void repeatedTakeoverAttemptsAreRefusedForAsLongAsTrafficContinues() {
        SessionRegistry reg = registry(32);
        reg.register("op-1", open(), T0);
        for (int i = 0; i < 20; i++) {
            long now = T0 + i * 1_000L;
            reg.touch("op-1", now);
            assertEquals(SessionRegistry.Admission.REJECTED_DUPLICATE,
                    reg.register("op-1", open(), now + 500));
        }
    }

    @Test
    void aStaleSessionCanBeReplacedSoReconnectionStillWorks() throws Exception {
        // A socket left behind by a dropped network looks identical to an active
        // one until it stops sending. Refusing those would break ordinary
        // reconnection, so silence for longer than the grace period is the signal.
        SessionRegistry reg = registry(32);
        WebSocketSession dead = open();
        reg.register("op-1", dead, T0);

        WebSocketSession fresh = open();
        assertEquals(SessionRegistry.Admission.ACCEPTED_REPLACED_STALE,
                reg.register("op-1", fresh, T0 + GRACE_MS + 1));

        assertEquals(fresh, reg.get("op-1"));
        // Assert the *call*, not the mock's isOpen() flag: a Mockito stub returns
        // whatever it was told to and never changes state, so checking isOpen()
        // here would assert nothing about the code under test.
        verify(dead).close();
    }

    @Test
    void theGraceBoundaryIsInclusiveAndNotOffByOne() {
        SessionRegistry reg = registry(32);
        reg.register("op-1", open(), T0);
        assertEquals(SessionRegistry.Admission.REJECTED_DUPLICATE,
                reg.register("op-1", open(), T0 + GRACE_MS - 1));
        assertEquals(SessionRegistry.Admission.ACCEPTED_REPLACED_STALE,
                reg.register("op-1", open(), T0 + GRACE_MS));
    }

    @Test
    void aClosedIncumbentIsReplacedImmediatelyWithoutWaitingForGrace() throws Exception {
        SessionRegistry reg = registry(32);
        WebSocketSession closed = open();
        when(closed.isOpen()).thenReturn(false);
        reg.register("op-1", closed, T0);

        assertEquals(SessionRegistry.Admission.ACCEPTED_REPLACED_STALE,
                reg.register("op-1", open(), T0 + 1));
    }

    @Test
    void aSessionWithNoActivityRecordIsTreatedAsStaleRatherThanWedgingTheOperator() {
        // Reached after an in-place upgrade leaves a socket registered without
        // bookkeeping. The safe failure is to let the operator back in, not to
        // lock them out of their own console.
        SessionRegistry reg = registry(32);
        assertEquals(SessionRegistry.Admission.ACCEPTED, reg.register("op-1", open(), T0));
        reg.remove("op-1");                       // drops the activity record only
        assertEquals(SessionRegistry.Admission.ACCEPTED, reg.register("op-1", open(), T0));
    }

    @Test
    void distinctSessionIdsAreUnaffected() {
        SessionRegistry reg = registry(32);
        assertEquals(SessionRegistry.Admission.ACCEPTED, reg.register("op-1", open(), T0));
        assertEquals(SessionRegistry.Admission.ACCEPTED, reg.register("op-2", open(), T0));
        assertEquals(2, reg.activeCount());
    }

    // -- the capacity rule -------------------------------------------------

    @Test
    void theSessionCountIsBounded() {
        SessionRegistry reg = registry(3);
        for (int i = 0; i < 3; i++) {
            assertEquals(SessionRegistry.Admission.ACCEPTED,
                    reg.register("op-" + i, open(), T0));
        }
        assertEquals(SessionRegistry.Admission.REJECTED_CAPACITY,
                reg.register("op-overflow", open(), T0));
        assertEquals(3, reg.activeCount(), "a rejected connection must add no state");
    }

    @Test
    void closingASessionReturnsItsCapacity() {
        SessionRegistry reg = registry(2);
        reg.register("op-1", open(), T0);
        reg.register("op-2", open(), T0);
        assertEquals(SessionRegistry.Admission.REJECTED_CAPACITY,
                reg.register("op-3", open(), T0));

        reg.remove("op-1");
        assertEquals(SessionRegistry.Admission.ACCEPTED, reg.register("op-3", open(), T0));
        assertEquals(2, reg.activeCount());
    }

    @Test
    void replacingAStaleSessionDoesNotConsumeExtraCapacity() {
        // At the ceiling with one dead session: reconnecting to that same id must
        // work, or a full gateway becomes unrecoverable without a restart.
        SessionRegistry reg = registry(1);
        reg.register("op-1", open(), T0);
        assertEquals(SessionRegistry.Admission.ACCEPTED_REPLACED_STALE,
                reg.register("op-1", open(), T0 + GRACE_MS + 1));
        assertEquals(1, reg.activeCount(), "still one session, not two");
    }

    @Test
    void capacityIsReportedForMonitoring() {
        SessionRegistry reg = registry(7);
        assertEquals(7, reg.capacity());
    }

    @Test
    void aDegenerateCapacityIsClampedRatherThanDisablingTheService() {
        // A zero or negative setting must not make the gateway unusable.
        assertEquals(1, registry(0).capacity());
        assertEquals(1, registry(-5).capacity());
    }

    // -- lifecycle and diagnostics -----------------------------------------

    @Test
    void removalClearsBothTheSocketAndItsActivityRecord() {
        SessionRegistry reg = registry(4);
        reg.register("op-1", open(), T0);
        reg.touch("op-1", T0 + 1_000);
        reg.remove("op-1");

        assertEquals(0, reg.activeCount());
        assertEquals(-1L, reg.idleMs("op-1", T0 + 2_000),
                "a removed session must not leave an activity entry behind");
        // Re-registering after removal must start clean, not inherit the old clock.
        assertEquals(SessionRegistry.Admission.ACCEPTED, reg.register("op-1", open(), T0 + 3_000));
        assertEquals(0L, reg.idleMs("op-1", T0 + 3_000));
    }

    @Test
    void idleTimeIsMeasuredFromTheLastFrame() {
        SessionRegistry reg = registry(4);
        reg.register("op-1", open(), T0);
        assertEquals(0L, reg.idleMs("op-1", T0));
        reg.touch("op-1", T0 + 2_000);
        assertEquals(0L, reg.idleMs("op-1", T0 + 2_000));
        assertEquals(3_000L, reg.idleMs("op-1", T0 + 5_000));
    }

    @Test
    void activityFromAnUnknownSessionIsIgnored() {
        SessionRegistry reg = registry(4);
        reg.touch("never-registered", T0);
        assertEquals(-1L, reg.idleMs("never-registered", T0));
        assertEquals(0, reg.activeCount());
    }

    @Test
    void closingASessionTwiceIsHarmless() {
        SessionRegistry reg = registry(4);
        reg.register("op-1", open(), T0);
        reg.remove("op-1");
        reg.remove("op-1");
        assertEquals(0, reg.activeCount());
    }
}
