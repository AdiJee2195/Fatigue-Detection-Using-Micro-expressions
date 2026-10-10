import pytest
from pydantic import ValidationError
from fatigue_xai.schemas import FrameTelemetry
from fatigue_xai.guard import validate

FACTS = {
    "risk_level": "HIGH", "fatigue_probability_percent": 72, "trend": "rising", "confidence": "high",
    "early_drift_note": "",
    "findings": ["the eyes were closed 18% of the last 10 s (AU43)",
                 "2 yawns in the last 5 minutes, the latest 20 s ago (AU26/AU27)"],
    "data_quality_notes": [],
}
GOOD = ("Risk is HIGH and rising, with the fatigue probability at 72%. The eyes were closed 18% of the last 10 s (AU43) "
        "and there were 2 yawns in the last 5 minutes, the latest 20 s ago (AU26/AU27).")


# ---------------- schema ----------------
def test_p_fatigue_derived_3class():
    f = FrameTelemetry(ts=1, class_probs={"microsleep": .2, "neutral": .5, "yawning": .3})
    assert f.p_fatigue == pytest.approx(.5)

def test_p_fatigue_derived_4class():
    f = FrameTelemetry(ts=1, class_probs={"alert": .4, "early_fatigue": .3, "yawning": .2, "microsleep": .1})
    assert f.p_fatigue == pytest.approx(.6)

def test_au_keys_normalised_and_clamped():
    f = FrameTelemetry(ts=1, class_probs={"neutral": 1.0}, au={"au04": 2, "7": 9})
    assert f.au == {"AU4": 2.0, "AU7": 5.0}

@pytest.mark.parametrize("probs", [{"neutral": .5}, {"foo": 1.0}, {}])
def test_bad_probs_rejected(probs):
    with pytest.raises(ValidationError):
        FrameTelemetry(ts=1, class_probs=probs)

def test_bad_au_key():
    with pytest.raises(ValidationError):
        FrameTelemetry(ts=1, class_probs={"neutral": 1.0}, au={"brow": 1})


# ---------------- guard ----------------
def test_good_text_passes():
    r = validate(GOOD, FACTS)
    assert r.passed, r.violations

def test_rounding_tolerance():
    assert validate(GOOD.replace("72%", "72.0%"), FACTS).passed

@pytest.mark.parametrize("mutate,expect", [
    (lambda t: t.replace("72%", "85%"), "not in facts"),
    (lambda t: t.replace("18%", "28%"), "not in facts"),
    (lambda t: t.replace("20 s", "45 s"), "not in facts"),
    (lambda t: t + " The brows were lowered (AU4).", "AU4 not in facts"),
    (lambda t: t.replace("HIGH", "CRITICAL"), "risk level"),
    (lambda t: t.replace("rising", "falling"), "trend"),
    (lambda t: t + " The driver should pull over.", "forbidden"),
    (lambda t: t + " This may indicate sleep apnea.", "forbidden"),
    (lambda t: t + " The pulse was elevated.", "signal not in facts"),
    (lambda t: "**" + t + "**", "markdown"),
    (lambda t: t + " I recommend a break.", "forbidden"),
    (lambda t: "Risk is HIGH and rising at 72%.", "omits"),
    (lambda t: "HIGH.", "short"),
    (lambda t: t.replace("Risk is HIGH", "Risk is elevated"), "does not state"),
    (lambda t: t + " The operator seems angry.", "forbidden"),
])
def test_hallucinations_rejected(mutate, expect):
    r = validate(mutate(GOOD), FACTS)
    assert not r.passed
    assert any(expect in v for v in r.violations), r.violations

def test_guard_detection_rate_is_total():
    muts = [GOOD.replace("72%", f"{x}%") for x in (10, 50, 71, 73, 90)] + \
           [GOOD.replace("18%", f"{x}%") for x in (8, 17, 19, 30)]
    assert all(not validate(m, FACTS).passed for m in muts)
