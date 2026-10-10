"""Provenance: synthetic readings must never be described as a real observation.

The failure this prevents is subtle and has nothing to do with the LLM being
wrong. Every number can be correct, the rule engine can be right, and the prose
can still be false — because it says "the driver's eyes" about numbers that were
generated. Correctness of the arithmetic does not imply truth of the sentence.

So provenance is checked at four levels: the schema, the engine, the prompt, and
the composer/guard.
"""
import asyncio
import json

import pytest

from fatigue_xai.guard import validate
from fatigue_xai.narrative import (
    LLM_GUARD_REJECTED,
    SOURCE_FALLBACK,
    SOURCE_TEMPLATE,
    NarrativeConfig,
    NarrativeService,
    compose,
    template_body,
)
from fatigue_xai.prompt import facts_view
from fatigue_xai.rules import FacsRuleEngine
from fatigue_xai.schemas import (
    MODE_CLASS_LIVE,
    MODE_CLASS_SYNTHETIC,
    MODE_CLASS_UNKNOWN,
    MODE_ONNX,
    MODE_STUB,
    Assessment,
    FrameTelemetry,
)
from fatigue_xai.simulator import generate

PROBS = {"microsleep": 0.3, "neutral": 0.5, "yawning": 0.2}


def frame(ts=1.0, **kw):
    return FrameTelemetry(session_id="s", ts=ts, class_probs=PROBS, **kw)


def run(coro):
    return asyncio.run(coro)


def synthetic_assessment():
    """A real assessment computed from the replayed (synthetic) scenario."""
    frames, *_ = generate("drift_to_microsleep")
    engine = FacsRuleEngine("sim")
    last = None
    for f in frames:
        if f.ts > 184:
            break
        last = engine.ingest(f) or last
    return last


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

def test_provenance_is_optional_and_absent_means_unstated():
    """A producer that does not know must be able to say nothing, not 'live'."""
    f = frame()
    assert f.mode is None and f.mode_class is None and f.signal_status is None
    assert f.provenance_is_synthetic() is False


def test_the_canonical_field_decides_when_present():
    assert frame(mode_class=MODE_CLASS_SYNTHETIC).provenance_is_synthetic() is True
    assert frame(mode_class=MODE_CLASS_LIVE).provenance_is_synthetic() is False
    assert frame(mode_class=MODE_CLASS_UNKNOWN).provenance_is_synthetic() is False


def test_a_legacy_producer_is_still_understood():
    """An un-upgraded gateway sends only ``mode``; the answer must still be honest."""
    assert frame(mode=MODE_STUB).provenance_is_synthetic() is True
    assert frame(mode=MODE_ONNX).provenance_is_synthetic() is False


def test_the_canonical_field_wins_over_the_legacy_one():
    f = frame(mode=MODE_STUB, mode_class=MODE_CLASS_LIVE)
    assert f.provenance_is_synthetic() is False


@pytest.mark.parametrize("raw", ["SYNTHETIC", "  synthetic  ", "Synthetic"])
def test_the_vocabulary_is_normalised(raw):
    assert frame(mode_class=raw).mode_class == MODE_CLASS_SYNTHETIC


def test_an_unrecognised_value_degrades_to_unknown_not_to_live():
    """A newer producer's vocabulary must not become an accidental live claim."""
    f = frame(mode_class="replayed")
    assert f.mode_class == MODE_CLASS_UNKNOWN
    assert f.provenance_is_synthetic() is False


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------

def test_the_engine_copies_provenance_onto_the_assessment():
    engine = FacsRuleEngine("s")
    out = None
    for i in range(40):
        out = engine.ingest(frame(ts=float(i) * 0.5, mode=MODE_STUB,
                                  mode_class=MODE_CLASS_SYNTHETIC,
                                  signal_status={"ear": "synthetic"})) or out
    assert out is not None
    assert out.mode == MODE_STUB
    assert out.mode_class == MODE_CLASS_SYNTHETIC
    assert out.signal_status == {"ear": "synthetic"}
    assert out.is_synthetic() is True


def test_a_later_frame_without_provenance_does_not_erase_it():
    """Omitting a field is 'no news', not 'retract what you said'."""
    engine = FacsRuleEngine("s")
    out = None
    for i in range(40):
        extra = ({"mode": MODE_STUB, "mode_class": MODE_CLASS_SYNTHETIC}
                 if i == 0 else {})
        out = engine.ingest(frame(ts=float(i) * 0.5, **extra)) or out
    assert out is not None and out.is_synthetic() is True


def test_live_provenance_produces_a_non_synthetic_assessment():
    engine = FacsRuleEngine("s")
    out = None
    for i in range(40):
        out = engine.ingest(frame(ts=float(i) * 0.5, mode=MODE_ONNX,
                                  mode_class=MODE_CLASS_LIVE)) or out
    assert out is not None
    assert out.is_synthetic() is False
    assert out.provenance_summary() == ""


def test_provenance_survives_the_assessment_serialisation():
    a = synthetic_assessment()
    payload = json.loads(a.model_dump_json())
    assert payload["mode_class"] == MODE_CLASS_SYNTHETIC
    assert payload["signal_status"]["ear"] == "synthetic"


# ---------------------------------------------------------------------------
# the composer owns the label — not the model
# ---------------------------------------------------------------------------

def test_compose_marks_a_synthetic_narrative():
    a = synthetic_assessment()
    text = compose(a, "Risk is HIGH.")
    assert "SIMULATION" in text
    assert "not a real driver" in text
    assert text.startswith(f"[{a.ts_str}] HIGH RISK - ")


def test_compose_does_not_mark_a_live_narrative():
    a = synthetic_assessment().model_copy(update={"mode_class": MODE_CLASS_LIVE})
    text = compose(a, "Risk is HIGH.")
    assert "SIMULATION" not in text
    assert text.startswith(f"[{a.ts_str}] HIGH RISK - Risk is HIGH.")


def test_the_marker_is_on_every_narrative_path():
    """Template, LLM, and guard fallback must all carry it."""
    a = synthetic_assessment()
    # 1. the deterministic template
    assert "SIMULATION" in compose(a, template_body(a))

    # 2. the LLM path
    from fatigue_xai.llm import ScriptedLLM
    good = ("Risk is HIGH and stable, with the fatigue probability at "
            f"{facts_view(a)['fatigue_probability_percent']}%. "
            + "; ".join(facts_view(a)["findings"]) + ".")
    llm_ev = run(NarrativeService(ScriptedLLM([good])).narrate(a))
    assert "SIMULATION" in llm_ev["text"]

    # 3. the fallback (provider fails)
    from fatigue_xai.llm import LLMError, ErrorKind
    failed = run(NarrativeService(ScriptedLLM([""], fail=True)).narrate(a))
    assert failed["source"] in (SOURCE_FALLBACK, SOURCE_TEMPLATE)
    assert "SIMULATION" in failed["text"]


def test_a_model_cannot_suppress_the_marker():
    """Even a model that writes confidently about a person gets the marker."""
    a = synthetic_assessment()
    from fatigue_xai.llm import ScriptedLLM
    defiant = ("Risk is HIGH and stable. The driver is clearly fatigued and the "
               "eye openness measurement shows 40% closure.")
    ev = run(NarrativeService(ScriptedLLM([defiant])).narrate(a))
    assert "SIMULATION" in ev["text"]
    assert ev["llm_status"] == LLM_GUARD_REJECTED
    assert ev["source"] == SOURCE_FALLBACK
    # The rejected sentence must not appear anywhere in what the operator reads.
    assert "clearly fatigued" not in ev["text"]
    assert "the driver" not in ev["text"].replace("not a real driver", "")


# ---------------------------------------------------------------------------
# the guard enforces the harm, not a password
# ---------------------------------------------------------------------------

def _facts(a):
    return facts_view(a)


def test_guard_rejects_synthetic_data_described_as_a_person():
    a = synthetic_assessment()
    facts = _facts(a)
    body = (f"Risk is {a.risk_level}. The driver was monitored and we measured "
            f"the fatigue probability at {facts['fatigue_probability_percent']}%.")
    res = validate(body, facts)
    assert not res.passed
    assert any("real observation" in x for x in res.violations), res.violations


@pytest.mark.parametrize("phrase", [
    "the driver", "a real driver", "the actual driver", "we observed",
    "we measured", "a real person", "real-time measurement",
])
def test_guard_catches_each_way_of_claiming_a_measurement(phrase):
    a = synthetic_assessment()
    facts = _facts(a)
    body = (f"Risk is {a.risk_level} and stable. Regarding {phrase}, the fatigue "
            f"probability is {facts['fatigue_probability_percent']}%.")
    res = validate(body, facts)
    assert not res.passed, f"{phrase!r} should have been rejected"


def test_the_same_wording_is_fine_when_the_data_is_live():
    """The check is about provenance, not about vocabulary."""
    a = synthetic_assessment().model_copy(update={"mode_class": MODE_CLASS_LIVE})
    facts = _facts(a)
    body = (f"Risk is {a.risk_level} and stable, with the fatigue probability at "
            f"{facts['fatigue_probability_percent']}%. " + "; ".join(facts["findings"]) + ".")
    assert validate(body, facts).passed, validate(body, facts).violations


def test_guard_does_not_demand_the_word_synthetic():
    """The marker is the composer's job; a good demo-mode answer must survive."""
    a = synthetic_assessment()
    facts = _facts(a)
    body = (f"Risk is {a.risk_level} and stable, with the fatigue probability at "
            f"{facts['fatigue_probability_percent']}%. " + "; ".join(facts["findings"]) + ".")
    res = validate(body, facts)
    assert res.passed, res.violations
    assert "synthetic" not in body.lower()


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------

def test_the_prompt_is_told_what_kind_of_data_it_has():
    a = synthetic_assessment()
    prov = facts_view(a)["provenance"]
    assert prov["data_class"] == MODE_CLASS_SYNTHETIC
    assert prov["pipeline"] == MODE_STUB
    assert prov["signal_provenance"], "each signal's origin must be stated"


def test_the_prompt_never_exposes_raw_telemetry_field_names():
    """The narrator is briefed in prose, not handed the telemetry structure."""
    a = synthetic_assessment()
    blob = json.dumps(facts_view(a))
    for field in ("class_probs", "rppg_bpm", "signal_status", "mode_class",
                  "au_intensities", "active_aus"):
        assert field not in blob, f"{field} leaked into the prompt"


def test_unavailable_signals_are_stated_as_such_to_the_model():
    a = synthetic_assessment()
    phrases = facts_view(a)["provenance"]["signal_provenance"]
    assert "pulse: not configured" in phrases
    assert not any("pulse: measured" in p for p in phrases)


def test_the_system_prompt_contains_a_provenance_rule():
    from fatigue_xai.prompt import SYSTEM_PROMPT
    assert "PROVENANCE" in SYSTEM_PROMPT
    assert "synthetic" in SYSTEM_PROMPT
    assert "the driver" in SYSTEM_PROMPT  # named as a phrase to avoid


# ---------------------------------------------------------------------------
# the demo path is labelled automatically
# ---------------------------------------------------------------------------

def test_the_replayed_demo_scenario_labels_its_own_output():
    """Reviewers run demo mode first; it must not need manual labelling."""
    a = synthetic_assessment()
    assert a.is_synthetic() is True
    assert a.provenance_summary().startswith("SIMULATION")


@pytest.mark.parametrize("scenario", [
    "drift_to_microsleep", "alert_steady", "yawn_only", "occlusion", "night_low_light",
])
def test_every_shipped_scenario_is_labelled(scenario):
    frames, *_ = generate(scenario)
    assert frames, "the scenario must produce frames"
    assert all(f.provenance_is_synthetic() for f in frames[:50])
