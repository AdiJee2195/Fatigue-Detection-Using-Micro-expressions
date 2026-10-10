"""Deterministic synthetic telemetry for demo mode.

Purpose: the whole pipeline — gateway, backpressure, rule engine, guard, narrative
stream — must be demonstrable on a laptop with no GPU, no model file and no LLM.
This generator makes that possible without a camera.

What it is NOT: a fatigue detector, and never a claim about a driver. Every row it
produces is labelled ``mode: "stub"`` and ``class_probs: "synthetic"``, and the
dashboard must show that.

The *ordering* of the signals is the point. Eyelid droop and brow/lid strain rise
from roughly 30 % of the drive, while the synthetic class probability only climbs
past ~50 %. That reproduces the pre-attentive premise the project is built on (and
exercises the engine's ``early_drift`` path), instead of a model that is merely
late to the same conclusion.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict

from .telemetry import LABELS

#: Defaults; overridable through STUB_CYCLE_S / STUB_RAMP_S.
DEFAULT_CYCLE_S = 120.0
DEFAULT_RAMP_S = 75.0

#: Baseline eye openness the droop is measured against.
BASELINE_EAR = 0.30


def stub_drive(elapsed_s: float, cycle_s: float = DEFAULT_CYCLE_S,
               ramp_s: float = DEFAULT_RAMP_S) -> float:
    """Deterministic 0..1 fatigue drive: ramps over ``ramp_s``, resets each ``cycle_s``.

    A sawtooth rather than a single ramp means a long demo repeatedly escalates and
    recovers, which also exercises the engine's downgrade hysteresis on the way down.
    """
    if cycle_s <= 0 or ramp_s <= 0:
        return 0.0
    return min(1.0, (max(0.0, float(elapsed_s)) % cycle_s) / ramp_s)


@dataclass(frozen=True)
class StubSignals:
    """One synthetic frame's signals."""

    class_probs: Dict[str, float]
    p_fatigue: float
    ear: float
    au: Dict[str, float]


def stub_signals(elapsed_s: float, rng: random.Random,
                 cycle_s: float = DEFAULT_CYCLE_S, ramp_s: float = DEFAULT_RAMP_S) -> StubSignals:
    """Synthetic signals for a point in the drive cycle."""
    drive = stub_drive(elapsed_s, cycle_s, ramp_s)
    noise = rng.gauss

    # Pre-attentive: lids droop and the brow/eyelid tighten before the model reacts.
    ear = BASELINE_EAR - 0.10 * drive + noise(0.0, 0.004)
    au4 = max(0.0, 0.2 + 2.4 * min(1.0, drive / 0.70) + noise(0.0, 0.15))
    au7 = max(0.0, 0.2 + 2.2 * min(1.0, drive / 0.75) + noise(0.0, 0.15))
    au43 = max(0.0, 0.3 + 3.0 * min(1.0, max(0.0, (drive - 0.55) / 0.45)) + noise(0.0, 0.10))

    # Occasional yawn episodes once the drive is high.
    yawning = drive > 0.6 and (int(elapsed_s) % 20) < 4
    au26 = max(0.0, (4.2 if yawning else 0.2) + noise(0.0, 0.20))

    p_micro = max(0.005, 0.02 + 0.50 * drive * drive + noise(0.0, 0.010))
    p_yawn = max(0.005, 0.02 + (0.30 if yawning else 0.05 * drive) + noise(0.0, 0.010))
    p_neutral = max(0.005, 1.0 - p_micro - p_yawn)
    total = p_micro + p_yawn + p_neutral
    probs = {
        "microsleep": p_micro / total,
        "neutral": p_neutral / total,
        "yawning": p_yawn / total,
    }
    # Sanity: the engine rejects probabilities that do not look like a distribution.
    assert set(probs) == set(LABELS)

    return StubSignals(
        class_probs=probs,
        p_fatigue=probs["microsleep"] + probs["yawning"],
        ear=min(0.5, max(0.02, ear)),
        au={"AU4": au4, "AU7": au7, "AU43": min(5.0, au43), "AU26": min(5.0, au26)},
    )
