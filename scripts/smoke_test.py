"""End-to-end smoke test: drive the whole pipeline and report PASS/FAIL per step.

    python scripts/smoke_test.py
    python scripts/smoke_test.py --url ws://localhost:8080/ws/video/smoke-1 --seconds 20
    python scripts/smoke_test.py --require-narrative false     # telemetry-only quick check

What it proves, in order:

    CONNECT         the gateway accepts the WebSocket handshake
    FRAME SEND      valid frames are accepted without the session dying
    ENVELOPE        every message is a well-formed {type, sessionId, payload, timestamp}
    TELEMETRY       telemetry arrives and satisfies the contract
    NARRATIVE       the narrative path is alive (and produces a final narrative)
    BACKPRESSURE    frames are dropped rather than queued, and every frame is accounted for
    ERROR HANDLING  malformed / empty / oversized frames are rejected in isolation
    HARD LIMIT      a payload above the WebSocket container's ceiling closes the socket
    DISCONNECT      a clean close releases the session on the gateway

Two frame ceilings exist and behave differently, which is why there are two checks.
`gateway.max-frame-bytes` (default 512 KB) is the handler's limit: a frame above it is
rejected and the session carries on. The container ceiling is that limit plus 64 KB of
headroom: a message above *that* is never delivered to the handler, and Tomcat closes
the connection with 1009. The headroom is what makes an ordinary client mistake cost a
frame rather than the session.

The backpressure check is the interesting one. The browser cannot see dropped frames —
they produce no message, by design — so the only honest way to verify drop-don't-queue
is to ask the gateway. This script sends a burst far faster than the pipeline can
answer, then reads `GET /sessions` and checks that the counters add up:

    framesReceived == framesProcessed + framesDropped + unparseable + telemetryErrors

`unparseable` counts frames that were received but never dispatched (bad JSON);
`telemetryErrors` counts frames the vision service failed on (received, not processed,
not dropped). Anything left over is an accounting bug. A run in which the pipeline
simply kept up with the burst is still a PASS — the invariant is that nothing is
silently lost, and the report says which case occurred.

Exit codes:
    0  every required check passed
    1  a check failed, or the gateway was unreachable
    2  a dependency is missing
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import websockets
except ImportError:  # pragma: no cover
    print("missing dependency: websockets   (pip install websockets)", file=sys.stderr)
    sys.exit(2)

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    print("missing dependency: pillow       (pip install pillow)", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass
class Check:
    name: str
    status: str = SKIP
    detail: str = ""
    required: bool = True


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)
    extras: List[str] = field(default_factory=list)

    def record(self, name: str, status: str, detail: str = "", required: bool = True) -> None:
        self.checks.append(Check(name, status, detail, required))

    def print(self) -> None:
        width = max((len(c.name) for c in self.checks), default=0)
        for check in self.checks:
            print(f"{check.name:<{width}}  {check.status:<4}  {check.detail}")
        for extra in self.extras:
            print(extra)
        failed = [c for c in self.checks if c.status == FAIL and c.required]
        print()
        print(f"E2E RESULT: {FAIL if failed else PASS}")

    @property
    def ok(self) -> bool:
        return not any(c.status == FAIL and c.required for c in self.checks)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def tiny_jpeg_b64(width: int = 320, height: int = 240) -> str:
    """A real (if featureless) JPEG so the vision service has something to decode."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (118, 118, 118)).save(buffer, format="JPEG", quality=60)
    return base64.b64encode(buffer.getvalue()).decode()


def http_base_from_ws_url(url: str) -> str:
    """`ws://host:8080/ws/video/x` -> `http://host:8080` (mirrors the frontend helper)."""
    parts = urllib.parse.urlsplit(url)
    scheme = "https" if parts.scheme in ("wss", "https") else "http"
    return f"{scheme}://{parts.netloc}"


def session_id_from_url(url: str) -> Optional[str]:
    path = urllib.parse.urlsplit(url).path
    prefix = "/ws/video/"
    if not path.startswith(prefix) or len(path) <= len(prefix):
        return None
    return urllib.parse.unquote(path[len(prefix):])


def fetch_sessions(base: str, timeout: float = 5.0) -> List[Dict[str, Any]]:
    """Blocking read of `GET /sessions`; callers run it in a worker thread."""
    request = urllib.request.Request(f"{base}/sessions", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


CONTRACT_FIELDS = (
    "ts", "class_probs", "p_fatigue", "ear", "au", "face_detected", "illumination",
    "rppg_bpm", "risk_score", "au_intensities", "active_aus", "inference_latency_ms",
)


@dataclass
class Observed:
    """Everything the reader thread learns from the envelopes it receives."""

    counts: Counter = field(default_factory=Counter)
    envelope_errors: List[str] = field(default_factory=list)
    first_telemetry: Optional[Dict[str, Any]] = None
    first_telemetry_fields_missing: List[str] = field(default_factory=list)
    narrative_sources: List[str] = field(default_factory=list)
    assessment_levels: List[str] = field(default_factory=list)
    error_reasons: List[str] = field(default_factory=list)

    def note(self, raw: str) -> None:
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            self.counts["<unparseable-envelope>"] += 1
            self.envelope_errors.append("message was not JSON")
            return

        if not isinstance(envelope, dict):
            self.counts["<non-object>"] += 1
            self.envelope_errors.append("message was not a JSON object")
            return

        kind = envelope.get("type")
        if not isinstance(kind, str) or not kind:
            self.envelope_errors.append("envelope had no type")
            return
        self.counts[kind] += 1

        # Every outbound message must carry the four envelope fields.
        for key in ("type", "sessionId", "payload", "timestamp"):
            if key not in envelope:
                self.envelope_errors.append(f"{kind}: missing {key}")
        if not isinstance(envelope.get("timestamp"), (int, float)):
            self.envelope_errors.append(f"{kind}: timestamp is not numeric")

        payload = envelope.get("payload") or {}

        if kind == "telemetry" and self.first_telemetry is None:
            self.first_telemetry = payload
            self.first_telemetry_fields_missing = [
                name for name in CONTRACT_FIELDS if name not in payload
            ]
        if kind == "narrative_final":
            self.narrative_sources.append(str(payload.get("source")))
        if kind == "assessment":
            body = payload.get("assessment") or payload
            level = body.get("risk_level")
            if level:
                self.assessment_levels.append(str(level))
        if kind in ("telemetry_error", "narrative_error"):
            self.error_reasons.append(str(payload.get("reason")))


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
@dataclass
class Options:
    url: str
    frames: int
    fps: float
    burst: int
    tail_s: float
    require_narrative: bool
    oversized_bytes: int
    hard_limit_bytes: int


async def run(options: Options) -> Report:
    report = Report()
    observed = Observed()
    payload = tiny_jpeg_b64()
    session_id = session_id_from_url(options.url) or "unknown"
    http_base = http_base_from_ws_url(options.url)

    # Counters we must reconcile against the gateway's own numbers.
    unparseable_sent = 0
    oversized_sent = 0
    empty_sent = 0

    # ---- CONNECT ------------------------------------------------------------
    try:
        socket = await websockets.connect(options.url, max_size=None, open_timeout=10)
    except Exception as error:  # noqa: BLE001 - any failure here is a failed connect
        report.record("CONNECT", FAIL, f"could not connect to {options.url}: {error}")
        return report
    report.record("CONNECT", PASS, f"handshake with {options.url}")

    reader_task = asyncio.create_task(_read_forever(socket, observed))

    try:
        # ---- FRAME SEND -----------------------------------------------------
        interval = 1.0 / max(1.0, options.fps)
        started = time.perf_counter()
        sent_ok = 0
        try:
            for _ in range(options.frames):
                await socket.send(json.dumps({
                    "frame": payload,
                    "timestamp": int(time.time() * 1000),
                }))
                sent_ok += 1
                await asyncio.sleep(interval)
        except Exception as error:  # noqa: BLE001
            report.record("FRAME SEND", FAIL, f"send failed after {sent_ok} frames: {error}")
            return report
        elapsed = time.perf_counter() - started
        report.record(
            "FRAME SEND", PASS,
            f"{sent_ok} valid frames in {elapsed:.1f}s ({sent_ok / max(elapsed, 1e-6):.1f} fps)",
        )

        # Give the pipeline a moment to drain before the boundary tests.
        await asyncio.sleep(1.0)

        # ---- ERROR HANDLING -------------------------------------------------
        # Each of these must be rejected on its own without disturbing the session.
        try:
            await socket.send("{this is not valid json")
            unparseable_sent += 1
            await socket.send(json.dumps({"frame": payload, "timestamp": "not-a-number"}))
            unparseable_sent += 1

            await socket.send("")
            empty_sent += 1

            # Over the handler's limit (gateway.max-frame-bytes, default 512 KB) but
            # inside the WebSocket container's headroom: the frame must be rejected on
            # its own, and the session must survive. A payload above the container
            # ceiling is a different case, checked separately below.
            await socket.send(json.dumps({
                "frame": "A" * options.oversized_bytes,
                "timestamp": int(time.time() * 1000),
            }))
            oversized_sent += 1

            # The session must still work afterwards.
            before = observed.counts["telemetry"] + observed.counts["telemetry_error"]
            for _ in range(5):
                await socket.send(json.dumps({
                    "frame": payload,
                    "timestamp": int(time.time() * 1000),
                }))
                await asyncio.sleep(interval)
            await asyncio.sleep(1.0)
            after = observed.counts["telemetry"] + observed.counts["telemetry_error"]
            survived = after > before
            report.record(
                "ERROR HANDLING", PASS if survived else FAIL,
                ("malformed/empty/oversized frames rejected; "
                 f"session survived ({after - before} further results)")
                if survived else "the session stopped producing results after bad frames",
            )
        except Exception as error:  # noqa: BLE001
            report.record("ERROR HANDLING", FAIL, f"boundary frames broke the session: {error}")

        # ---- BACKPRESSURE ---------------------------------------------------
        burst_before = observed.counts["telemetry"] + observed.counts["telemetry_error"]
        burst_started = time.perf_counter()
        for _ in range(options.burst):
            await socket.send(json.dumps({
                "frame": payload,
                "timestamp": int(time.time() * 1000),
            }))
        burst_elapsed = time.perf_counter() - burst_started
        # Let the in-flight frame settle so the counters are stable when we read them.
        await asyncio.sleep(2.0)

        try:
            rows = await asyncio.to_thread(fetch_sessions, http_base)
        except Exception as error:  # noqa: BLE001
            rows = []
            report.record("BACKPRESSURE", FAIL, f"could not read {http_base}/sessions: {error}")

        if rows:
            row = next((r for r in rows if r.get("sessionId") == session_id), None)
            if row is None:
                report.record("BACKPRESSURE", FAIL, f"session {session_id!r} not listed by /sessions")
            else:
                received = int(row.get("framesReceived", 0))
                processed = int(row.get("framesProcessed", 0))
                dropped = int(row.get("framesDropped", 0))
                telemetry_errors = observed.counts["telemetry_error"]
                unaccounted = received - processed - dropped - unparseable_sent - telemetry_errors
                # An in-flight frame is legitimate; anything more is a leak.
                account_ok = 0 <= unaccounted <= 1
                detail = (
                    f"received={received} processed={processed} dropped={dropped} "
                    f"unparseable={unparseable_sent} vision_errors={telemetry_errors} "
                    f"unaccounted={unaccounted}"
                )
                if not account_ok:
                    detail += " — frames are not accounted for"
                elif dropped > 0:
                    detail += " — drop-don't-queue observed"
                else:
                    detail += " — pipeline kept up with the burst; no drops needed"
                report.record("BACKPRESSURE", PASS if account_ok else FAIL, detail)
                report.record(
                    "BURST", PASS if burst_elapsed < interval * options.burst else FAIL,
                    f"{options.burst} frames sent in {burst_elapsed * 1000:.0f} ms "
                    f"(vs {interval * options.burst * 1000:.0f} ms paced) — "
                    f"{observed.counts['telemetry'] + observed.counts['telemetry_error'] - burst_before} "
                    "resulting messages",
                )

        # ---- NARRATIVE ------------------------------------------------------
        # The engine needs roughly a minute of telemetry to learn a baseline, so keep
        # reading; the tail is deliberately generous.
        await asyncio.sleep(options.tail_s)

        narrative_events = sum(
            observed.counts[kind] for kind in (
                "narrative_start", "narrative_chunk", "narrative_retry",
                "narrative_final", "narrative_cancelled",
            )
        )
        if narrative_events > 0:
            report.record(
                "NARRATIVE", PASS,
                f"{narrative_events} narrative events; sources={observed.narrative_sources or 'none yet'}",
            )
        else:
            report.record(
                "NARRATIVE", FAIL,
                "no narrative events — is the narrative engine running and reachable?",
                required=options.require_narrative,
            )

        if observed.assessment_levels:
            report.record(
                "ASSESSMENT", PASS,
                f"{len(observed.assessment_levels)} assessments; levels seen: "
                f"{sorted(set(observed.assessment_levels))}",
            )
        else:
            report.record(
                "ASSESSMENT", FAIL,
                "the rule engine produced no assessment — it needs ~60 s of usable telemetry",
                required=False,
            )

        # ---- HARD LIMIT -----------------------------------------------------
        # Last, because it is expected to tear the connection down. Above the
        # WebSocket container's ceiling the frame is never delivered to the handler:
        # the container closes the socket with 1009. That backstop is what bounds the
        # memory a client can make the gateway hold, and it is the reason the two
        # limits behave differently. Informational: it depends on server config.
        try:
            await socket.send(json.dumps({
                "frame": "A" * options.hard_limit_bytes,
                "timestamp": int(time.time() * 1000),
            }))
            killed = await _wait_closed(socket, timeout=3.0)
            report.record(
                "HARD LIMIT", PASS if killed else SKIP,
                (f"container closed the connection for a {options.hard_limit_bytes:,}-char "
                 "payload above its ceiling")
                if killed else
                (f"a {options.hard_limit_bytes:,}-char payload did not trip the container "
                 "ceiling on this server; the handler limit is still enforced"),
                required=False,
            )
        except Exception as error:  # noqa: BLE001
            report.record("HARD LIMIT", PASS, f"connection refused it outright: {error}", required=False)
    finally:
        # ---- DISCONNECT -----------------------------------------------------
        try:
            await socket.close(code=1000, reason="smoke test complete")
        except Exception:  # noqa: BLE001
            pass

        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

        # The gateway must release the session once the socket closes.
        released = False
        for _ in range(10):
            try:
                rows = await asyncio.to_thread(fetch_sessions, http_base)
            except Exception:  # noqa: BLE001
                rows = []
            if not any(r.get("sessionId") == session_id for r in rows):
                released = True
                break
            await asyncio.sleep(0.5)
        report.record(
            "DISCONNECT", PASS if released else FAIL,
            f"session {session_id!r} released by the gateway" if released
            else f"session {session_id!r} is still registered after a clean close",
        )
        if empty_sent or oversized_sent:
            report.extras.append(
                f"  (boundary frames sent: unparseable={unparseable_sent} "
                f"empty={empty_sent} oversized={oversized_sent} — the gateway rejects these "
                "without counting them as received)"
            )
        if observed.counts:
            report.extras.append("  envelopes received:")
            for kind, count in sorted(observed.counts.items()):
                report.extras.append(f"    {kind:<22} {count}")
        if observed.error_reasons:
            report.extras.append(f"  error reasons: {sorted(set(observed.error_reasons))}")

    # ---- envelope / telemetry validation (printed in the requested order) ----
    if observed.envelope_errors:
        report.record(
            "ENVELOPE", FAIL,
            f"{len(observed.envelope_errors)} malformed envelopes, e.g. {observed.envelope_errors[:3]}",
        )
    else:
        total = sum(observed.counts.values())
        report.record("ENVELOPE", PASS, f"{total} messages, all with the four envelope fields")

    if observed.first_telemetry is None:
        report.record("TELEMETRY", FAIL, "no telemetry envelope — check the vision service URL")
    elif observed.first_telemetry_fields_missing:
        report.record(
            "TELEMETRY", FAIL,
            f"telemetry is missing contract fields: {observed.first_telemetry_fields_missing}",
        )
    else:
        first = observed.first_telemetry
        ts = first.get("ts")
        ts_ok = isinstance(ts, (int, float)) and 0 < ts < 1e10
        if not ts_ok:
            report.record("TELEMETRY", FAIL, f"ts is not epoch seconds: {ts!r}")
        else:
            report.record(
                "TELEMETRY", PASS,
                f"{observed.counts['telemetry']} rows; mode={first.get('mode')} "
                f"p_fatigue={first.get('p_fatigue')} "
                f"face_detected={first.get('face_detected')}",
            )

    # Reorder to the documented sequence for a readable report.
    order = ["CONNECT", "FRAME SEND", "ENVELOPE", "TELEMETRY", "NARRATIVE", "ASSESSMENT",
             "BACKPRESSURE", "BURST", "ERROR HANDLING", "HARD LIMIT", "DISCONNECT"]
    report.checks.sort(key=lambda c: order.index(c.name) if c.name in order else len(order))
    return report


async def _wait_closed(socket, timeout: float = 3.0) -> bool:
    """True once the socket reports itself closed, within the timeout.

    `websockets` exposes the protocol state either as an enum (whose `.name` is
    CONNECTING/OPEN/CLOSING/CLOSED) or, depending on version, as a plain integer
    whose `str()` is the number. Checking only for the word would silently report
    "still open" after the server had closed the connection, so both are handled.
    """
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        state = getattr(socket, "state", None)
        label = getattr(state, "name", None) or str(state)
        if str(label).strip().upper() == "CLOSED":
            return True
        await asyncio.sleep(0.1)
    return False


async def _read_forever(socket, observed: Observed) -> None:
    try:
        async for raw in socket:
            observed.note(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the socket closed; the report reflects what was seen
        return


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="ws://localhost:8080/ws/video/smoke-1",
                        help="gateway session URL (its path segment is the session id)")
    parser.add_argument("--frames", type=int, default=1200)
    parser.add_argument("--seconds", type=float, default=None,
                        help="alternative to --frames: run for this many seconds")
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--burst", type=int, default=80,
                        help="frames sent as fast as possible for the backpressure check")
    parser.add_argument("--tail-seconds", type=float, default=20.0,
                        help="keep reading after the last frame (narratives need ~60 s total)")
    parser.add_argument("--oversized-bytes", type=int, default=526_000,
                        help="JSON payload size for the graceful-rejection check "
                             "(default 526000 chars: above gateway.max-frame-bytes=524288, "
                             "inside the container headroom)")
    parser.add_argument("--hard-limit-bytes", type=int, default=700_000,
                        help="JSON payload size for the container-ceiling check "
                             "(default 700000 chars: above max-frame-bytes plus the 64 KB "
                             "container headroom, so the container closes the socket)")
    parser.add_argument("--require-narrative", default="true",
                        help="true|false — fail when no narrative event arrives")
    args = parser.parse_args()

    frames = args.frames
    if args.seconds is not None:
        frames = max(1, int(args.seconds * args.fps))

    require = str(args.require_narrative).lower() not in ("false", "0", "no")
    options = Options(
        url=args.url,
        frames=frames,
        fps=args.fps,
        burst=args.burst,
        tail_s=args.tail_seconds,
        require_narrative=require,
        oversized_bytes=args.oversized_bytes,
        hard_limit_bytes=args.hard_limit_bytes,
    )

    print(f"smoke test -> {options.url}")
    print(f"sending {options.frames} frames at {options.fps} fps, then a {options.burst}-frame burst")
    print()
    try:
        report = asyncio.run(run(options))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except OSError as error:
        print(f"CONNECT  FAIL  {error}")
        print("\nE2E RESULT: FAIL")
        return 1

    # Summarise what arrived, then the checks.
    report.print()
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
