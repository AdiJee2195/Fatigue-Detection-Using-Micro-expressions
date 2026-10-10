package com.fatiguedetect.gateway.websocket;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Lightweight per-session metrics tracker with a bounded sliding window
 * for latency percentiles. No external dependencies — feeds directly into
 * structured log lines for benchmark report grepping.
 *
 * The deque is a bounded list (oldest evicted via pollFirst) rather than a
 * fixed-slot ring buffer; access is synchronized because the window is read by
 * the periodic metrics ticker on a different thread than the WebFlux callback.
 */
public class FrameMetrics {

    private static final int WINDOW_SIZE = 200;

    private final AtomicLong framesReceived = new AtomicLong();
    private final AtomicLong framesProcessed = new AtomicLong();
    private final AtomicLong framesDropped = new AtomicLong();

    /** Wall-clock ms of the first and most recent processed frame — for effective_fps. */
    private final AtomicLong firstProcessedWallMs = new AtomicLong(0L);
    private final AtomicLong lastProcessedWallMs = new AtomicLong(0L);

    /** Sliding window of recent latencies (ms). */
    private final Deque<Long> latencyWindow = new ArrayDeque<>(WINDOW_SIZE);

    public void recordReceived() {
        framesReceived.incrementAndGet();
    }

    public void recordProcessed(long latencyMs) {
        long now = System.currentTimeMillis();
        framesProcessed.incrementAndGet();
        firstProcessedWallMs.compareAndSet(0L, now);
        lastProcessedWallMs.set(now);
        synchronized (latencyWindow) {
            if (latencyWindow.size() >= WINDOW_SIZE) {
                latencyWindow.pollFirst();
            }
            latencyWindow.addLast(latencyMs);
        }
    }

    public void recordDropped() {
        framesDropped.incrementAndGet();
    }

    public long getFramesReceived() { return framesReceived.get(); }
    public long getFramesProcessed() { return framesProcessed.get(); }
    public long getFramesDropped() { return framesDropped.get(); }

    /** Average latency over the sliding window, or -1 if no data. */
    public double getAvgLatencyMs() {
        synchronized (latencyWindow) {
            if (latencyWindow.isEmpty()) return -1;
            return latencyWindow.stream().mapToLong(Long::longValue).average().orElse(-1);
        }
    }

    /** 95th-percentile latency over the sliding window, or -1 if no data. */
    public double getP95LatencyMs() {
        synchronized (latencyWindow) {
            if (latencyWindow.isEmpty()) return -1;
            long[] sorted = latencyWindow.stream().mapToLong(Long::longValue).sorted().toArray();
            int idx = (int) Math.ceil(0.95 * sorted.length) - 1;
            return sorted[Math.max(0, idx)];
        }
    }

    /**
     * Achieved end-to-end frame rate, or -1 if fewer than two frames have been
     * processed. With the drop-if-busy policy this equals 1/vision-latency, so
     * it is the number to watch: the rule engine's blink/PERCLOS logic assumes
     * roughly 15-24 fps, and a gap longer than gap_reset_s restarts its windows.
     */
    public double getEffectiveFps() {
        long first = firstProcessedWallMs.get();
        long last = lastProcessedWallMs.get();
        long processed = framesProcessed.get();
        if (first == 0L || processed < 2 || last <= first) {
            return -1;
        }
        return (processed - 1) * 1000.0 / (last - first);
    }

    /** Structured summary line for periodic or on-close logging. */
    public String toSummaryString(String sessionId) {
        return String.format(
                "sessionId=%s frames_received=%d frames_processed=%d frames_dropped=%d "
                        + "avg_latency_ms=%.1f p95_latency_ms=%.1f effective_fps=%.1f",
                sessionId, getFramesReceived(), getFramesProcessed(), getFramesDropped(),
                getAvgLatencyMs(), getP95LatencyMs(), getEffectiveFps()
        );
    }
}
