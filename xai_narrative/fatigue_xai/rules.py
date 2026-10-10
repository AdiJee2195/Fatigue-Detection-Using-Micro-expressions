"""Deterministic FACS rule engine.

Everything the LLM is later allowed to say originates here.  The engine turns a
stream of FrameTelemetry into an ``Assessment`` (risk level, trend, findings with
pre-rendered plain-language clauses).  No LLM is involved, so the same input
always yields the same assessment.

IMPORTANT: every threshold in ``RuleConfig`` is a *starting heuristic*, not a
validated clinical cut-off.  Tune them on DROZY / UTA-RLDD / NTHU-DDD (see
README, "Calibrating thresholds") before reporting results.
"""
from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

from .schemas import LEVELS, UNKNOWN, Assessment, Finding, FrameTelemetry, format_ts


@dataclass
class RuleConfig:
    # --- windows / cadence
    window_s: float = 10.0            # sliding window for rates/means
    assess_every_s: float = 2.0       # how often an Assessment is produced
    history_s: float = 300.0          # horizon for yawn / long-closure event counts
    trend_s: float = 180.0            # horizon for trend slope
    p_ema_tau_s: float = 5.0          # smoothing time-constant of p_fatigue
    gap_reset_s: float = 1.0          # frame gap that resets episode detectors
    downgrade_hold: int = 5           # consecutive lower assessments before stepping down

    # --- eye closure (EAR / AU43)
    ear_closed_abs: float = 0.20      # used until a per-user baseline exists
    ear_closed_rel: float = 0.75      # closed if EAR < rel * baseline mean
    blink_max_s: float = 0.5          # closures shorter than this are blinks
    long_closure_s: Tuple[float, float] = (0.5, 1.0)   # severity 2, 3
    perclos: Tuple[float, float] = (0.15, 0.30)        # severity 2, 3
    perclos_window_s: float = 30.0    # PERCLOS needs a longer window than the 10 s rate window
    perclos_min_s: float = 15.0

    # --- model probabilities
    p_micro_mean: Tuple[float, float] = (0.35, 0.60)   # severity 2, 3 (window mean)

    # --- pre-attentive signals
    lid_droop_ratio: Tuple[float, float] = (0.88, 0.80)   # EAR mean / baseline mean <= (sev1, sev2)
    stare_var_ratio: Tuple[float, float] = (0.60, 0.40)   # window EAR var / baseline var <= (sev1, sev2)
    low_blink_rate: float = 8.0       # blinks/min
    high_blink_rate: float = 30.0     # blinks/min
    blink_horizon_s: float = 30.0
    blink_min_observed_s: float = 15.0
    au4_mean: Tuple[float, float] = (1.2, 2.2)        # brow lowerer (sev1, sev2)
    au7_mean: Tuple[float, float] = (1.2, 2.2)        # lid tightener
    au_sustained_frac: float = 0.6

    # --- yawning
    yawn_p: float = 0.60
    yawn_au: float = 3.0              # AU26/AU27 intensity
    yawn_min_s: float = 1.0
    yawn_merge_gap_s: float = 0.6
    yawn_counts: Tuple[int, int, int] = (1, 2, 3)      # severity 1, 2, 3 within history_s

    # --- trend
    trend_slope: float = 0.10         # per-minute change in smoothed p_fatigue
    trend_min_span_s: float = 30.0

    # --- level mapping (smoothed p_fatigue)
    level_p: Tuple[float, float, float] = (0.35, 0.60, 0.85)

    # --- data quality
    min_face_ratio: float = 0.70
    min_illumination: float = 0.12
    baseline_s: float = 60.0
    baseline_p_max: float = 0.30      # only "alert-looking" frames feed the baseline

    actions: Dict[str, str] = field(default_factory=lambda: {
        "LOW": "No action required.",
        "MODERATE": "Monitor closely and plan a rest stop soon.",
        "HIGH": "Advise the operator to take a break at the next safe opportunity.",
        "CRITICAL": "Immediate alert: the operator should stop at the nearest safe location.",
        UNKNOWN: "Check camera position and lighting.",
    })

    @classmethod
    def from_json(cls, path: str) -> "RuleConfig":
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        known = {f.name for f in fields(cls)}
        bad = set(raw) - known
        if bad:
            raise ValueError(f"unknown RuleConfig keys: {sorted(bad)}")
        for k, v in list(raw.items()):
            if isinstance(getattr(cls(), k), tuple):
                raw[k] = tuple(v)
        return cls(**raw)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
class EpisodeDetector:
    """Online detector of contiguous 'active' episodes with merge-gap tolerance."""

    def __init__(self, grace_s: float = 0.0):
        self.grace = grace_s
        self.start: Optional[float] = None
        self.last_active: float = 0.0
        self.dt: float = 1 / 24

    def reset(self) -> None:
        self.start = None

    def update(self, ts: float, active: bool, dt: float) -> Optional[Tuple[float, float]]:
        """Returns (start, duration) when an episode just ended."""
        self.dt = dt
        if active:
            if self.start is None:
                self.start = ts
            self.last_active = ts
            return None
        if self.start is not None and ts - self.last_active > self.grace:
            ep = (self.start, self.last_active - self.start + dt)
            self.start = None
            return ep
        return None

    def ongoing(self) -> float:
        return 0.0 if self.start is None else self.last_active - self.start + self.dt


class BaselineTracker:
    """Per-user EAR baseline from alert-looking frames (mean + window variance)."""

    def __init__(self, cfg: RuleConfig):
        self.cfg = cfg
        self._vals: List[float] = []
        self._t0: Optional[float] = None
        self._chunk_means: List[float] = []
        self._chunk_vars: List[float] = []
        self.mean: Optional[float] = None
        self.var: Optional[float] = None
        self.ready = False
        self._n_chunks_needed = max(2, int(round(cfg.baseline_s / cfg.window_s)))

    def set_manual(self, mean: float, var: float) -> None:
        self.mean, self.var, self.ready = mean, var, True

    def update(self, f: FrameTelemetry) -> None:
        if self.ready or f.ear is None or not f.face_detected or f.p_fatigue > self.cfg.baseline_p_max:
            return
        if self._t0 is None:
            self._t0 = f.ts
        self._vals.append(f.ear)
        if f.ts - self._t0 >= self.cfg.window_s:
            a = np.asarray(self._vals)
            self._chunk_means.append(float(a.mean()))
            self._chunk_vars.append(float(a.var()))
            self._vals, self._t0 = [], None
            if len(self._chunk_means) >= self._n_chunks_needed:
                self.mean = float(np.mean(self._chunk_means))
                self.var = float(np.median(self._chunk_vars))
                self.ready = True

    @property
    def progress(self) -> float:
        return 1.0 if self.ready else min(1.0, len(self._chunk_means) / self._n_chunks_needed)


def _sev_hi(value: float, thresholds: Tuple[float, ...]) -> int:
    """Highest severity index whose threshold is <= value (0 if none)."""
    return sum(1 for t in thresholds if value >= t)


def _sev_lo(value: float, thresholds: Tuple[float, ...]) -> int:
    """Severity when LOWER is worse: thresholds descending."""
    return sum(1 for t in thresholds if value <= t)


def _r(x: float, nd: int = 1) -> float:
    return round(float(x), nd)


# ----------------------------------------------------------------------------
# engine
# ----------------------------------------------------------------------------
class FacsRuleEngine:
    """Per-session stateful rule engine.  ``ingest`` returns an Assessment every
    ``assess_every_s`` seconds of frame time (else None)."""

    def __init__(self, session_id: str = "default", cfg: Optional[RuleConfig] = None):
        self.session_id = session_id
        self.cfg = cfg or RuleConfig()
        self.baseline = BaselineTracker(self.cfg)
        self._win: Deque[FrameTelemetry] = deque()
        self._prev_ts: Optional[float] = None
        self._dt = 1 / 24
        self._first_ts: Optional[float] = None
        self._blink_obs_start: Optional[float] = None
        self._p_ema: Optional[float] = None
        self._closure = EpisodeDetector(0.0)
        self._yawn = EpisodeDetector(self.cfg.yawn_merge_gap_s)
        self._blinks: Deque[float] = deque()
        self._closed_hist: Deque[Tuple[float, bool]] = deque()
        self._long_closures: Deque[Tuple[float, float]] = deque()   # (end_ts, dur)
        self._yawns: Deque[Tuple[float, float]] = deque()           # (start_ts, dur)
        self._p_hist: Deque[Tuple[float, float]] = deque()
        self._last_assess_ts: Optional[float] = None
        self._level = 0
        self._lower_count = 0
        self.history: Deque[Assessment] = deque(maxlen=500)
        # Provenance declared by the producer.  None means "not stated", which the
        # assessment reports as unknown rather than assuming a measurement.
        self._mode: Optional[str] = None
        self._mode_class: Optional[str] = None
        self._signal_status: Dict[str, str] = {}

    # -- per-frame state -------------------------------------------------
    def _is_closed(self, f: FrameTelemetry) -> Optional[bool]:
        if f.ear is not None:
            thr = self.cfg.ear_closed_rel * self.baseline.mean if self.baseline.ready else self.cfg.ear_closed_abs
            return f.ear < thr
        au43 = f.au_value("AU43")
        if au43 is not None:
            return au43 >= 3.0
        return None  # cannot tell

    def _is_yawning(self, f: FrameTelemetry) -> bool:
        au = max(f.au_value("AU26") or 0.0, f.au_value("AU27") or 0.0)
        return f.prob("yawning") >= self.cfg.yawn_p or au >= self.cfg.yawn_au

    def ingest(self, f: FrameTelemetry) -> Optional[Assessment]:
        c = self.cfg
        # Provenance travels with the frames, so it is captured here and copied onto
        # every assessment.  Taken from the most recent frame that declared it: a
        # producer that omits it must not erase what an earlier frame stated.
        if f.mode is not None:
            self._mode = f.mode
        if f.mode_class is not None:
            self._mode_class = f.mode_class
        if f.signal_status:
            self._signal_status = dict(f.signal_status)
        if self._prev_ts is not None:
            if f.ts < self._prev_ts:
                return None  # out-of-order frame: drop
            gap = f.ts - self._prev_ts
            if gap > c.gap_reset_s:
                self._closure.reset()
                self._yawn.reset()
                self._restart_blink_observation()
            elif gap > 0:
                self._dt = 0.9 * self._dt + 0.1 * gap
        if self._first_ts is None:
            self._first_ts = f.ts
        dt = self._dt
        self._prev_ts = f.ts

        # smoothed model probability
        if self._p_ema is None:
            self._p_ema = f.p_fatigue
        else:
            a = 1 - math.exp(-dt / c.p_ema_tau_s)
            self._p_ema += a * (f.p_fatigue - self._p_ema)

        self.baseline.update(f)

        if f.face_detected:
            closed = self._is_closed(f)
            if closed is not None:
                self._closed_hist.append((f.ts, closed))
                ep = self._closure.update(f.ts, closed, dt)
                if ep:
                    start, dur = ep
                    if dur < c.blink_max_s:
                        self._blinks.append(start)
                    else:
                        self._long_closures.append((start + dur, dur))
            ep = self._yawn.update(f.ts, self._is_yawning(f), dt)
            if ep and ep[1] >= c.yawn_min_s:
                self._yawns.append(ep)
        else:
            self._closure.reset()
            self._yawn.reset()
            self._restart_blink_observation()

        if f.face_detected and self._blink_obs_start is None:
            self._blink_obs_start = f.ts
        self._win.append(f)
        while self._win and f.ts - self._win[0].ts > c.window_s:
            self._win.popleft()
        while self._closed_hist and f.ts - self._closed_hist[0][0] > c.perclos_window_s:
            self._closed_hist.popleft()
        while self._blinks and f.ts - self._blinks[0] > c.blink_horizon_s:
            self._blinks.popleft()
        while self._long_closures and f.ts - self._long_closures[0][0] > c.history_s:
            self._long_closures.popleft()
        while self._yawns and f.ts - self._yawns[0][0] > c.history_s:
            self._yawns.popleft()

        if self._last_assess_ts is None:
            self._last_assess_ts = f.ts
            return None
        if f.ts - self._last_assess_ts < c.assess_every_s:
            return None
        self._last_assess_ts = f.ts
        return self._assess(f.ts)

    # -- assessment ------------------------------------------------------
    def _assess(self, ts: float) -> Assessment:
        c = self.cfg
        win = list(self._win)
        n = len(win)
        span = (win[-1].ts - win[0].ts) if n > 1 else 0.0
        face_ratio = sum(1 for x in win if x.face_detected) / max(1, n)
        visible = [x for x in win if x.face_detected]

        # ---- quality ----
        notes: List[str] = []
        reliable = True
        if face_ratio < c.min_face_ratio:
            reliable = False
            notes.append(f"face visible in only {_r(face_ratio * 100, 0):.0f}% of the last {c.window_s:.0f} s")
        ill = [x.illumination for x in visible if x.illumination is not None]
        if ill and float(np.mean(ill)) < c.min_illumination:
            notes.append("lighting is very low, so readings are less certain")
        have_ear = any(x.ear is not None for x in visible)
        calibrating = have_ear and not self.baseline.ready
        if calibrating:
            notes.append("still learning this operator's normal eye openness")

        signals = ["model_probabilities"]
        if have_ear:
            signals.append("ear")
        if any(x.au for x in visible):
            signals.append("facs_au")
        if any(x.rppg_bpm is not None for x in visible):
            signals.append("rppg")

        p_now = float(np.mean([x.p_fatigue for x in win[-5:]])) if win else 0.0
        p_ema = float(self._p_ema if self._p_ema is not None else p_now)

        # ---- trend ----
        self._p_hist.append((ts, p_ema))
        while self._p_hist and ts - self._p_hist[0][0] > c.trend_s:
            self._p_hist.popleft()
        trend, slope = "unknown", None
        if len(self._p_hist) >= 5 and self._p_hist[-1][0] - self._p_hist[0][0] >= c.trend_min_span_s:
            t = np.array([h[0] for h in self._p_hist])
            y = np.array([h[1] for h in self._p_hist])
            slope = float(np.polyfit((t - t[0]) / 60.0, y, 1)[0])
            trend = "rising" if slope >= c.trend_slope else "falling" if slope <= -c.trend_slope else "stable"

        findings: List[Finding] = []
        if visible and n >= 3:
            findings += self._eye_findings(visible, span, ts)
            findings += self._au_findings(visible)
            findings += self._model_findings(visible)
        findings += self._yawn_findings(ts)
        if trend == "rising" and p_ema >= 0.25:
            findings.append(Finding(
                id="RISING_TREND", label="Rising fatigue trend", severity=1,
                evidence={"slope_per_min": _r(slope, 2)},
                plain=f"the fatigue probability has been climbing for the last {_r(self._p_hist[-1][0] - self._p_hist[0][0], 0):.0f} s",
            ))

        # ---- level (transparent mapping) ----
        raw = sum(1 for t in c.level_p if p_ema >= t)
        strong = [f for f in findings if f.severity >= 2 and f.id != "RISING_TREND"]
        pre = [f for f in findings if f.pre_attentive and f.severity >= 1]
        early_drift = False
        if len(strong) >= 2:
            raw = min(2, raw + 1)   # corroboration can lift to HIGH, never to CRITICAL
        if any(f.id == "LONG_CLOSURE" for f in findings):
            raw = max(raw, 2)       # any closure >= 0.5 s while driving is at least HIGH
        if any(f.severity == 3 and f.id in ("LONG_CLOSURE", "MODEL_MICROSLEEP") for f in findings):
            raw = 3
        if raw < 1 and len(pre) >= 2:   # pre-attentive signs while the CV model is still quiet
            raw, early_drift = 1, True

        level_idx = self._apply_hysteresis(raw)
        level = UNKNOWN if not reliable else LEVELS[level_idx]
        raw_level = UNKNOWN if not reliable else LEVELS[raw]

        # ---- confidence ----
        sources = sum([p_ema >= c.level_p[0], bool([f for f in findings if f.id != "RISING_TREND"])])
        if not reliable or calibrating or "lighting" in " ".join(notes):
            confidence = "low"
        elif level_idx >= 1 and sources == 2:
            confidence = "high"
        else:
            confidence = "medium" if level_idx >= 1 or sources else "high"

        a = Assessment(
            session_id=self.session_id, ts=ts, ts_str=format_ts(ts), window_s=c.window_s,
            risk_level=level, raw_level=raw_level, p_fatigue_now=_r(p_now, 3),
            p_fatigue_smoothed=_r(p_ema, 3), trend=trend,
            trend_slope_per_min=None if slope is None else _r(slope, 3),
            confidence=confidence, early_drift=early_drift,
            findings=sorted(findings, key=lambda f: -f.severity), quality_notes=notes,
            reliable=reliable, calibrating=calibrating,
            recommended_action=c.actions[level], signals_available=signals,
            mode=self._mode, mode_class=self._mode_class,
            signal_status=dict(self._signal_status),
        )
        self.history.append(a)
        return a

    def _apply_hysteresis(self, raw: int) -> int:
        if raw >= self._level:
            self._level, self._lower_count = raw, 0
        else:
            self._lower_count += 1
            if self._lower_count >= self.cfg.downgrade_hold:
                self._level -= 1
                self._lower_count = 0
        return self._level

    # -- rule groups -----------------------------------------------------
    def _eye_findings(self, vis: List[FrameTelemetry], span: float, ts: float) -> List[Finding]:
        c, out = self.cfg, []
        ears = np.array([x.ear for x in vis if x.ear is not None])
        closed_flags = [self._is_closed(x) for x in vis]
        known = [x for x in closed_flags if x is not None]

        if known:
            longest = max(
                [d for (end, d) in self._long_closures if ts - end <= c.window_s] + [self._closure.ongoing()]
            )
            s = _sev_hi(longest, c.long_closure_s)
            if s:
                out.append(Finding(
                    id="LONG_CLOSURE", label="Extended eye closure", facs=["AU43"], severity=s + 1,
                    evidence={"longest_closure_s": _r(longest, 1)},
                    plain=f"the eyes stayed closed for {longest:.1f} s in a row (AU43)",
                ))
            hist = self._closed_hist
            if hist and hist[-1][0] - hist[0][0] >= c.perclos_min_s:
                perclos = sum(1 for _, x in hist if x) / len(hist)
                s = _sev_hi(perclos, c.perclos)
                if s:
                    out.append(Finding(
                        id="PERCLOS_ELEVATED", label="Eyes closed a large share of the time", facs=["AU43"],
                        severity=s + 1, evidence={"perclos_percent": _r(perclos * 100, 0)},
                        plain=f"the eyes were closed {perclos * 100:.0f}% of the last {c.perclos_window_s:.0f} s (AU43)",
                    ))

        if len(ears) >= 3 and self.baseline.ready:
            ratio = float(ears.mean() / self.baseline.mean)
            s = _sev_lo(ratio, c.lid_droop_ratio)
            if s:
                out.append(Finding(
                    id="LID_DROOP", label="Heavy eyelids", facs=["AU43"], severity=s, pre_attentive=True,
                    evidence={"ear_vs_baseline_percent": _r(ratio * 100, 0)},
                    plain=f"the eyes are open only {ratio * 100:.0f}% as wide as this operator's normal (AU43, partial)",
                ))
            vr = float(ears.var() / self.baseline.var) if self.baseline.var else 1.0
            rate = self._blink_rate(span)
            s = _sev_lo(vr, c.stare_var_ratio)
            low_blink = rate is not None and rate <= c.low_blink_rate
            if s and low_blink:   # variance drop alone is not enough: a blink-free 10 s window is normal ~6% of the time
                ev = {"lid_movement_variance_percent": _r(vr * 100, 0)}
                txt = f"eyelid movement variation has dropped to {vr * 100:.0f}% of normal, a fixed stare"
                if rate is not None:
                    ev["blinks_per_min"] = _r(rate, 0)
                    txt += f", with {rate:.0f} blinks per minute"
                out.append(Finding(id="FIXED_STARE", label="Fixed stare", severity=min(2, s + 1),
                                   pre_attentive=True, evidence=ev, plain=txt))

        rate = self._blink_rate(span)
        if rate is not None and rate >= c.high_blink_rate:
            out.append(Finding(
                id="RAPID_BLINKING", label="Rapid blinking", facs=["AU45"], severity=1, pre_attentive=True,
                evidence={"blinks_per_min": _r(rate, 0)},
                plain=f"blinking is frequent at {rate:.0f} blinks per minute (AU45)",
            ))
        return out

    def _restart_blink_observation(self) -> None:
        """Blinks cannot be counted while the face is hidden, so restart the clock."""
        self._blinks.clear()
        self._blink_obs_start = None

    def _blink_rate(self, span: float) -> Optional[float]:
        if self._blink_obs_start is None:
            return None
        observed = min(self.cfg.blink_horizon_s, (self._prev_ts or 0) - self._blink_obs_start)
        if observed < self.cfg.blink_min_observed_s:
            return None
        return len(self._blinks) * 60.0 / observed

    def _au_findings(self, vis: List[FrameTelemetry]) -> List[Finding]:
        c, out = self.cfg, []
        for code, thr, fid, label, txt in (
            ("AU4", c.au4_mean, "BROW_STRAIN", "Brow strain",
             "the brows are drawn together (AU4)"),
            ("AU7", c.au7_mean, "LID_TIGHTENING", "Eyelid tightening",
             "the eyelids are tightened (AU7)"),
        ):
            vals = np.array([x.au_value(code) for x in vis if x.au_value(code) is not None])
            if len(vals) < 3:
                continue
            mean = float(vals.mean())
            sustained = float((vals >= thr[0]).mean())
            s = _sev_hi(mean, thr)
            if s and sustained >= c.au_sustained_frac:
                out.append(Finding(
                    id=fid, label=label, facs=[code], severity=s, pre_attentive=True,
                    evidence={f"{code}_mean_intensity": _r(mean, 1), "sustained_percent": _r(sustained * 100, 0)},
                    plain=f"{txt}, average intensity {mean:.1f} out of 5",
                ))
        return out

    def _model_findings(self, vis: List[FrameTelemetry]) -> List[Finding]:
        c, out = self.cfg, []
        pm = float(np.mean([x.prob("microsleep") for x in vis]))
        s = _sev_hi(pm, c.p_micro_mean)
        if s:
            out.append(Finding(
                id="MODEL_MICROSLEEP", label="Model sees microsleep", severity=s + 1,
                evidence={"microsleep_probability_percent": _r(pm * 100, 0)},
                plain=f"the vision model rates microsleep at {pm * 100:.0f}% over the last {c.window_s:.0f} s",
            ))
        return out

    def _yawn_findings(self, ts: float) -> List[Finding]:
        c = self.cfg
        n = len(self._yawns)
        s = _sev_hi(n, c.yawn_counts)
        if not s:
            return []
        last_ago = ts - self._yawns[-1][0]
        mins = c.history_s / 60.0
        word = "yawn" if n == 1 else "yawns"
        return [Finding(
            id="YAWNING", label="Yawning", facs=["AU26", "AU27"], severity=s,
            evidence={"yawns_in_window": float(n), "last_yawn_seconds_ago": _r(last_ago, 0)},
            plain=f"{n} {word} in the last {mins:.0f} minutes, the latest {last_ago:.0f} s ago (AU26/AU27)",
        )]


def rule_catalogue(c: Optional[RuleConfig] = None) -> List[Dict]:
    """Human/LLM-readable description of every rule and its (tunable) thresholds."""
    c = c or RuleConfig()
    return [
        {"id": "LONG_CLOSURE", "facs": ["AU43"], "signal": "EAR or AU43", "pre_attentive": False,
         "rule": f"eyes closed in one stretch for >= {c.long_closure_s[0]} s (severity 2) or >= {c.long_closure_s[1]} s (severity 3)",
         "effect": "any occurrence sets risk >= HIGH; >= 1.0 s sets CRITICAL"},
        {"id": "PERCLOS_ELEVATED", "facs": ["AU43"], "signal": "EAR or AU43", "pre_attentive": False,
         "rule": f"share of the last {c.perclos_window_s:.0f} s with eyes closed >= {c.perclos[0]:.0%} (sev 2) / {c.perclos[1]:.0%} (sev 3)"},
        {"id": "MODEL_MICROSLEEP", "facs": [], "signal": "CNN class probabilities", "pre_attentive": False,
         "rule": f"window-mean P(microsleep) >= {c.p_micro_mean[0]} (sev 2) / {c.p_micro_mean[1]} (sev 3)"},
        {"id": "LID_DROOP", "facs": ["AU43"], "signal": "EAR + per-user baseline", "pre_attentive": True,
         "rule": f"window-mean EAR <= {c.lid_droop_ratio[0]:.0%} (sev 1) / {c.lid_droop_ratio[1]:.0%} (sev 2) of the operator's baseline"},
        {"id": "FIXED_STARE", "facs": [], "signal": "EAR variance + blink rate + baseline", "pre_attentive": True,
         "rule": f"window EAR variance <= {c.stare_var_ratio[0]:.0%} of baseline AND blink rate <= {c.low_blink_rate:.0f}/min"},
        {"id": "RAPID_BLINKING", "facs": ["AU45"], "signal": "EAR", "pre_attentive": True,
         "rule": f"blink rate >= {c.high_blink_rate:.0f}/min"},
        {"id": "BROW_STRAIN", "facs": ["AU4"], "signal": "FACS AU4", "pre_attentive": True,
         "rule": f"mean AU4 >= {c.au4_mean[0]} (sev 1) / {c.au4_mean[1]} (sev 2), present in >= {c.au_sustained_frac:.0%} of frames"},
        {"id": "LID_TIGHTENING", "facs": ["AU7"], "signal": "FACS AU7", "pre_attentive": True,
         "rule": f"mean AU7 >= {c.au7_mean[0]} (sev 1) / {c.au7_mean[1]} (sev 2), present in >= {c.au_sustained_frac:.0%} of frames"},
        {"id": "YAWNING", "facs": ["AU26", "AU27"], "signal": "CNN P(yawning) or AU26/27", "pre_attentive": False,
         "rule": f"yawn episodes (>= {c.yawn_min_s} s) in last {c.history_s / 60:.0f} min: {c.yawn_counts[0]} / {c.yawn_counts[1]} / {c.yawn_counts[2]} -> severity 1 / 2 / 3"},
        {"id": "RISING_TREND", "facs": [], "signal": "smoothed P(fatigue) slope", "pre_attentive": False,
         "rule": f"slope >= {c.trend_slope}/min over {c.trend_s:.0f} s while smoothed P(fatigue) >= 0.25"},
        {"id": "LEVEL_MAPPING", "facs": [], "signal": "all",
         "rule": f"base level from smoothed P(fatigue): >= {c.level_p[0]} MODERATE, >= {c.level_p[1]} HIGH, >= {c.level_p[2]} CRITICAL; "
                 "two or more severity>=2 findings lift one level (max HIGH); two pre-attentive findings force at least MODERATE (early drift); "
                 f"downgrades are held for {c.downgrade_hold} assessments"},
    ]
