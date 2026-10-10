"""Synthetic telemetry generator for development, tests and the evaluation harness.

It simulates the *signals* (EAR, AU intensities, class probabilities) that the
CV / EVM stages will eventually provide, with a known ground-truth timeline.
Results obtained on it demonstrate pipeline behaviour, NOT real-world accuracy.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

from .schemas import MODE_CLASS_SYNTHETIC, MODE_STUB, FrameTelemetry


@dataclass
class Phase:
    name: str
    t0: float
    t1: float
    expected_min_level: str     # lowest acceptable level during the phase (after settling)
    expected_max_level: str


def _smooth(a: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return a
    return np.convolve(a, np.ones(k) / k, mode="same")


def generate(scenario: str = "drift_to_microsleep", fps: float = 24.0, seed: int = 7,
             session_id: str = "sim") -> Tuple[List[FrameTelemetry], List[Phase], List[float]]:
    """Returns (frames, ground-truth phases, microsleep-event times)."""
    rng = np.random.RandomState(seed)
    if scenario == "alert_steady":
        T = 150.0
    elif scenario == "drift_to_microsleep":
        T = 245.0
    elif scenario == "yawn_only":
        T = 150.0
    elif scenario == "occlusion":
        T = 150.0
    elif scenario == "night_low_light":
        T = 150.0
    else:
        raise ValueError(f"unknown scenario {scenario}")
    n = int(T * fps)
    t = np.arange(n) / fps

    # --- latent fatigue drive (0..1) and phase table
    drive = np.zeros(n)
    phases: List[Phase] = []
    micro_events: List[float] = []
    yawn_times: List[float] = []
    closure_events: List[Tuple[float, float]] = []   # (start, dur)
    if scenario in ("alert_steady", "occlusion", "night_low_light"):
        phases.append(Phase("alert", 0, T, "LOW", "LOW"))
    elif scenario == "yawn_only":
        yawn_times = [70.0, 95.0, 120.0]
        phases = [Phase("alert", 0, 60, "LOW", "LOW"), Phase("yawning", 85, 150, "LOW", "HIGH")]
    else:
        drive = np.interp(t, [0, 90, 150, 200, 245], [0.0, 0.0, 0.45, 0.8, 1.0])
        yawn_times = [160.0, 176.0, 192.0]
        closure_events = [(205.0, 0.7), (214.0, 1.1), (222.0, 0.9), (232.0, 2.2), (240.0, 1.6)]
        micro_events = [c[0] for c in closure_events if c[1] >= 1.0]
        phases = [
            Phase("alert_baseline", 0, 90, "LOW", "LOW"),
            Phase("early_drift", 110, 150, "LOW", "HIGH"),
            Phase("yawning_and_drowsy", 165, 200, "MODERATE", "CRITICAL"),
            Phase("microsleep", 214, 245, "HIGH", "CRITICAL"),
        ]

    # --- EAR with blinks
    ear_base = 0.30 - 0.06 * drive
    blink_rate_per_min = np.interp(drive, [0, 0.45, 1.0], [17, 6, 10])
    ear = ear_base + rng.normal(0, 0.006, n) + rng.normal(0, 0.012, n) * (1 - 0.7 * drive)
    closed = np.zeros(n, dtype=bool)
    i = 0
    while i < n:
        rate = blink_rate_per_min[i] / 60.0 / fps
        if rng.rand() < rate:
            dur = int(rng.uniform(0.12, 0.30) * fps) + 1
            closed[i:i + dur] = True
            i += dur
        i += 1
    for (s, d) in closure_events:
        closed[int(s * fps):int((s + d) * fps)] = True
    ear = np.where(_smooth(closed.astype(float), 3) > 0.5, 0.08 + rng.normal(0, 0.01, n), ear)
    ear = np.clip(ear, 0.02, 0.5)

    # --- yawns
    yawning = np.zeros(n, dtype=bool)
    for y in yawn_times:
        yawning[int(y * fps):int((y + 4.0) * fps)] = True

    # --- AUs
    au4 = np.clip(0.3 + 2.6 * np.clip((drive - 0.1) / 0.6, 0, 1) + rng.normal(0, 0.25, n), 0, 5)
    au7 = np.clip(0.2 + 2.3 * np.clip((drive - 0.15) / 0.6, 0, 1) + rng.normal(0, 0.25, n), 0, 5)
    au43 = np.where(closed, 4.6, 0.4 + 1.2 * drive) + rng.normal(0, 0.2, n)
    au26 = np.where(yawning, 4.2, 0.2) + rng.normal(0, 0.2, n)

    # --- class probabilities (what the CNN would report)
    held = np.zeros(n)   # seconds the eyes have been closed so far
    run = 0.0
    for k in range(n):
        run = run + 1 / fps if closed[k] else 0.0
        held[k] = run
    p_micro = np.clip(np.where(held > 0.45, 0.75 + 0.2 * np.minimum(held, 2) / 2, 0.03 + 0.18 * drive) +
                      rng.normal(0, 0.04, n), 0.005, 0.98)
    p_yawn = np.clip(np.where(yawning, 0.85, 0.03 + 0.05 * drive) + rng.normal(0, 0.04, n), 0.005, 0.98)
    p_neutral = np.clip(1 - p_micro - p_yawn, 0.005, 1)

    illum = np.full(n, 0.5)
    face = np.ones(n, dtype=bool)
    if scenario == "occlusion":
        face[int(60 * fps):int(80 * fps)] = False
        face[int(110 * fps):int(113 * fps)] = False
        phases = [Phase("alert", 0, 55, "LOW", "LOW"), Phase("occluded", 70, 80, "UNKNOWN", "UNKNOWN"),
                  Phase("alert_after", 100, 150, "LOW", "LOW")]
    if scenario == "night_low_light":
        illum[:] = 0.08

    frames: List[FrameTelemetry] = []
    # Replayed frames declare their provenance. These are generated numbers, so the
    # assessments computed from them are labelled as a simulation rather than as
    # readings taken from a person — which is what makes this file reusable as a
    # demonstration without any claim having to be walked back later.
    provenance = {
        "mode": MODE_STUB,
        "mode_class": MODE_CLASS_SYNTHETIC,
        "signal_status": {
            "class_probs": "synthetic", "ear": "synthetic", "au": "synthetic",
            "rppg_bpm": "not_configured", "face_detection": "synthetic",
        },
    }
    for k in range(n):
        tot = p_micro[k] + p_yawn[k] + p_neutral[k]
        probs = {"microsleep": p_micro[k] / tot, "neutral": p_neutral[k] / tot, "yawning": p_yawn[k] / tot}
        if not face[k]:
            frames.append(FrameTelemetry(session_id=session_id, frame_id=k, ts=float(t[k]), class_probs={
                "microsleep": 0.05, "neutral": 0.9, "yawning": 0.05}, face_detected=False, **provenance))
            continue
        frames.append(FrameTelemetry(
            session_id=session_id, frame_id=k, ts=float(t[k]), class_probs=probs,
            ear=float(ear[k]),
            au={"AU4": float(au4[k]), "AU7": float(au7[k]), "AU43": float(np.clip(au43[k], 0, 5)),
                "AU26": float(np.clip(au26[k], 0, 5))},
            illumination=float(illum[k]),
            **provenance,
        ))
    return frames, phases, micro_events


def frames_iter(scenario: str, fps: float = 24.0, seed: int = 7) -> Iterator[FrameTelemetry]:
    yield from generate(scenario, fps, seed)[0]


async def _demo(scenario: str, speed: float, url: Optional[str]) -> None:  # pragma: no cover - interactive
    from .llm import llm_from_env
    from .narrative import NarrativeService
    from .session import OperatorSession

    async def emit(ev: Dict) -> None:
        if ev["type"] in ("narrative_final", "narrative_retry"):
            print(json.dumps({k: ev[k] for k in ev if k in ("type", "text", "source", "violations")}, indent=None))

    sess = OperatorSession("demo", NarrativeService(llm_from_env()), emit)
    frames, _, _ = generate(scenario)
    for f in frames:
        await sess.on_frame(f)
        if speed > 0:
            await asyncio.sleep(1 / 24 / speed)
        elif f.frame_id % 24 == 0:
            await asyncio.sleep(0)   # let narration tasks run when replaying faster than real time
    await sess.drain()


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser(description="Replay a synthetic scenario through the narrative pipeline")
    ap.add_argument("--scenario", default="drift_to_microsleep")
    ap.add_argument("--speed", type=float, default=0, help="1 = real time; 0 = as fast as possible")
    asyncio.run(_demo(ap.parse_args().scenario, ap.parse_args().speed, None))
