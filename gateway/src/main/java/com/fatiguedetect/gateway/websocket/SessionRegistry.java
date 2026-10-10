package com.fatiguedetect.gateway.websocket;

import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;

import java.io.IOException;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Thread-safe registry mapping sessionId → WebSocketSession, with two admission
 * rules that a permissive registry gets wrong.
 *
 * <h2>1. A duplicate sessionId is not a reconnect by default</h2>
 *
 * The sessionId is in the WebSocket URL, so it is public knowledge — the
 * documented default is {@code operator-001}. Previously a second connection
 * claiming an existing id simply replaced the first, which means anyone who could
 * guess or observe an id could both <em>read</em> that operator's telemetry and
 * narrative stream <em>and</em> evict them mid-session. The eviction is the part
 * that turns an eavesdropping bug into a denial-of-service one.
 *
 * <p>So a duplicate id is now <strong>rejected</strong> while the incumbent is
 * still active. It is only allowed when the incumbent has gone quiet for longer
 * than {@code gateway.session-takeover-grace-ms}, because a half-open connection
 * left behind by a dropped network is indistinguishable from an active one until
 * it stops sending, and refusing those would break ordinary reconnection.
 *
 * <p>The {@code Origin} check in {@link com.fatiguedetect.gateway.config.WebSocketConfig}
 * is not a substitute: browsers enforce it, and any non-browser client sets the
 * header to whatever it likes. An unguessable sessionId is the real control today;
 * see the security section of the README for what that does and does not buy.
 *
 * <h2>2. The session count is bounded</h2>
 *
 * Each session reserves a WebSocket text buffer of
 * {@code maxFrameBytes + envelope + container headroom} (~576 KiB by default), and
 * the per-session maps are unbounded. Without a cap, the memory a single
 * unauthenticated client can pin is a function of how many connections it opens.
 * The default cap is small on purpose: this is a single-operator research
 * console, not a multi-tenant service, and a rejected connection with a clear
 * close code is far easier to diagnose than an OOM.
 */
@Slf4j
@Component
public class SessionRegistry {

    /** The outcome of an admission attempt, so the caller can close with a reason. */
    public enum Admission {
        /** The session owns its id. */
        ACCEPTED,
        /** Replaced a session that had gone quiet; the incumbent was closed. */
        ACCEPTED_REPLACED_STALE,
        /** Refused: another live connection already owns this id. */
        REJECTED_DUPLICATE,
        /** Refused: the gateway is at its session ceiling. */
        REJECTED_CAPACITY
    }

    private final ConcurrentHashMap<String, WebSocketSession> sessions = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, Long> lastActivityMs = new ConcurrentHashMap<>();

    private final int maxSessions;
    private final long takeoverGraceMs;

    public SessionRegistry(@Value("${gateway.max-sessions:32}") int maxSessions,
                           @Value("${gateway.session-takeover-grace-ms:5000}") long takeoverGraceMs) {
        this.maxSessions = Math.max(1, maxSessions);
        this.takeoverGraceMs = Math.max(0, takeoverGraceMs);
        log.info("session admission: max_sessions={} takeover_grace_ms={}", this.maxSessions, this.takeoverGraceMs);
    }

    /**
     * Attempt to admit a session.
     *
     * @param sessionId   id from the WebSocket URL
     * @param session     the incoming connection
     * @param nowMs       current time, injected so tests do not sleep
     * @return what happened; the caller must close the socket unless ACCEPTED*
     */
    public Admission register(String sessionId, WebSocketSession session, long nowMs) {
        WebSocketSession incumbent = sessions.get(sessionId);
        if (incumbent != null && incumbent.isOpen() && !hasGoneQuiet(sessionId, nowMs)) {
            log.warn("sessionId={} admission rejected: a live connection already owns this id "
                            + "(ws_id={}); refusing to evict it", sessionId, incumbent.getId());
            return Admission.REJECTED_DUPLICATE;
        }

        // Count only what would actually be added: replacing a stale entry for the
        // same id does not consume new capacity.
        if (incumbent == null && sessions.size() >= maxSessions) {
            log.warn("sessionId={} admission rejected: at capacity ({}/{})",
                    sessionId, sessions.size(), maxSessions);
            return Admission.REJECTED_CAPACITY;
        }

        WebSocketSession previous = sessions.put(sessionId, session);
        lastActivityMs.put(sessionId, nowMs);
        boolean replacedStale = previous != null;
        if (replacedStale && previous.isOpen()) {
            log.warn("sessionId={} taking over a stale connection (no traffic for > {} ms), "
                    + "closing ws_id={}", sessionId, takeoverGraceMs, previous.getId());
            try {
                previous.close();
            } catch (IOException e) {
                log.debug("sessionId={} error closing stale session", sessionId, e);
            }
        }
        log.info("sessionId={} registered ws_session_id={} active={}/{}",
                sessionId, session.getId(), sessions.size(), maxSessions);
        return replacedStale ? Admission.ACCEPTED_REPLACED_STALE : Admission.ACCEPTED;
    }

    /** Record traffic, which is what keeps an incumbent from looking stale. */
    public void touch(String sessionId, long nowMs) {
        if (sessions.containsKey(sessionId)) {
            lastActivityMs.put(sessionId, nowMs);
        }
    }

    public void remove(String sessionId) {
        WebSocketSession removed = sessions.remove(sessionId);
        lastActivityMs.remove(sessionId);
        if (removed != null) {
            log.info("sessionId={} unregistered ws_session_id={} active={}/{}",
                    sessionId, removed.getId(), sessions.size(), maxSessions);
        }
    }

    public WebSocketSession get(String sessionId) {
        return sessions.get(sessionId);
    }

    /**
     * Whether {@code session} is the connection currently registered under this id.
     *
     * <p>Teardown must be guarded by this. Once a duplicate connection can be
     * <em>refused</em>, a refused socket still fires {@code afterConnectionClosed} —
     * and if cleanup were keyed on the sessionId alone, the act of rejecting an
     * intruder would deregister the legitimate operator's session, close its
     * narrative socket and drop its metrics. That is the same eviction this change
     * exists to prevent, arriving by a different path; an integration test caught
     * it.
     */
    public boolean isOwner(String sessionId, WebSocketSession session) {
        return session != null && sessions.get(sessionId) == session;
    }

    /**
     * Thread-safe send: checks isOpen() before writing to avoid ISE on closed sessions.
     * IOExceptions are logged and swallowed — the caller should never be disrupted
     * because a client disconnected.
     */
    public void sendToSession(String sessionId, String jsonPayload) {
        WebSocketSession session = sessions.get(sessionId);
        if (session == null) {
            log.debug("sessionId={} session not found, dropping outbound message", sessionId);
            return;
        }
        if (!session.isOpen()) {
            log.debug("sessionId={} session closed, dropping outbound message", sessionId);
            return;
        }
        try {
            synchronized (session) {
                session.sendMessage(new TextMessage(jsonPayload));
            }
        } catch (IOException e) {
            log.warn("sessionId={} failed to send message: {}", sessionId, e.getMessage());
        }
    }

    /** Returns an unmodifiable snapshot of all active session IDs. */
    public Set<String> activeSessionIds() {
        return Set.copyOf(sessions.keySet());
    }

    /** Live session count, for {@code /health}. */
    public int activeCount() {
        return sessions.size();
    }

    /** The configured ceiling, so monitoring can report headroom. */
    public int capacity() {
        return maxSessions;
    }

    /** Milliseconds since this session last sent anything; -1 when unknown. */
    public long idleMs(String sessionId, long nowMs) {
        Long last = lastActivityMs.get(sessionId);
        return last == null ? -1L : Math.max(0L, nowMs - last);
    }

    private boolean hasGoneQuiet(String sessionId, long nowMs) {
        Long last = lastActivityMs.get(sessionId);
        if (last == null) {
            // An incumbent with no recorded activity predates this registry's
            // bookkeeping; treat it as stale rather than wedging the operator out.
            return true;
        }
        return nowMs - last >= takeoverGraceMs;
    }
}
