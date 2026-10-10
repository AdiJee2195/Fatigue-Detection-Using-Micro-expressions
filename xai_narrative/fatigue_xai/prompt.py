"""Prompt construction.  The LLM never sees raw telemetry: it sees a small,
pre-rendered 'facts view' whose wording and numbers were produced by the rule
engine.  Its only job is to merge those clauses into fluent prose."""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from .schemas import Assessment

SYSTEM_PROMPT = """You write short Operator Risk Assessments for a driver-monitoring system. \
The reader is a non-technical fleet supervisor.

You receive FACTS as JSON. Turn them into plain prose.

STRICT RULES
1. Use ONLY information present in FACTS. Never add causes, diagnoses, emotions, medical conditions or details of the vehicle or road.
2. Every number you write must be copied from FACTS. Do not round differently, do not invent numbers.
3. Mention each item in "findings" using its own wording. Keep the AU code in brackets exactly as given, and never mention an AU code that is not in FACTS.
4. State the risk level exactly as given in "risk_level".
5. If "data_quality_notes" is not empty, say in one short clause that certainty is reduced.
6. Do NOT give advice, instructions or recommendations. The system adds the recommended action itself.
7. Do NOT mention a timestamp.
8. Output 2 or 3 sentences, at most 60 words, plain text only: no lists, no markdown, no quotation marks.
9. PROVENANCE. FACTS.provenance.data_class is 'synthetic' or 'live'.
   - If it is 'synthetic', these numbers were GENERATED, not measured. You must say so, and you must NOT describe them as an observation of a driver: no "the driver", no "we measured", no "real" anything. Write about what the rules detected in generated input.
   - If it is 'live', do not mention provenance at all; it needs no comment.
10. FACTS.provenance.signal_provenance lists what each measurement is: measured, generated, or absent. Never imply that a signal listed as anything other than "measured" was measured."""

_EX1_FACTS = {
    "risk_level": "HIGH", "fatigue_probability_percent": 72, "trend": "rising", "confidence": "high",
    "early_drift_note": "",
    "findings": [
        "the eyes were closed 18% of the last 10 s (AU43)",
        "2 yawns in the last 5 minutes, the latest 20 s ago (AU26/AU27)",
    ],
    "data_quality_notes": [],
}
_EX1_OUT = ("Risk is HIGH and rising, with the fatigue probability at 72%. The eyes were closed 18% of the "
            "last 10 s (AU43) and there were 2 yawns in the last 5 minutes, the latest 20 s ago (AU26/AU27).")

_EX2_FACTS = {
    "risk_level": "MODERATE", "fatigue_probability_percent": 31, "trend": "stable", "confidence": "low",
    "early_drift_note": "Early signs are present although the vision model's fatigue probability is still low.",
    "findings": [
        "the brows are drawn together (AU4), average intensity 2.4 out of 5",
        "eyelid movement variation has dropped to 38% of normal, a fixed stare",
    ],
    "data_quality_notes": ["still learning this operator's normal eye openness"],
}
_EX2_OUT = ("Risk is MODERATE and stable, with the fatigue probability at 31%, but early signs are showing: "
            "the brows are drawn together (AU4), average intensity 2.4 out of 5, "
            "and eyelid movement variation has dropped to 38% of normal, a fixed stare. Certainty is reduced because "
            "the system is still learning this operator's normal eye openness.")


MAX_FINDINGS = 3

#: Provenance reaches the prompt as *words*, not as the telemetry field names.
#:
#: Two reasons. First, the narrator is deliberately never given the raw telemetry
#: structure (see the module docstring and the test that enforces it): handing it
#: `signal_status` keyed by field name would re-expose exactly the internal names
#: the design keeps out of the prompt. Second, "eye openness: generated" is simply
#: a better instruction than `{"ear": "synthetic"}`. The guard still evaluates the
#: structured form, which never leaves the process.
_SIGNAL_LABELS = {
    "class_probs": "the fatigue classifier output",
    "ear": "eye openness",
    "au": "facial strain",
    "rppg_bpm": "pulse",
    "face_detection": "face detection",
    "reason": "frame quality",
}

_STATUS_LABELS = {
    "live": "measured",
    "synthetic": "generated, not measured",
    "estimated": "estimated",
    "unavailable": "unavailable",
    "not_detected": "nothing detected",
    "not_configured": "not configured",
    "unknown": "unknown origin",
}


def _signal_provenance(status: Optional[Dict[str, str]]) -> List[str]:
    """Human-readable provenance phrases, sorted for a stable prompt."""
    out: List[str] = []
    for key, value in sorted((status or {}).items()):
        label = _SIGNAL_LABELS.get(key, key.replace("_", " "))
        out.append(f"{label}: {_STATUS_LABELS.get(str(value).lower(), str(value))}")
    return out


def narrative_findings(a: Assessment):
    """Findings the narrator talks about: top-severity first, trend excluded (it is in the headline)."""
    fs = [f for f in a.findings if f.id != "RISING_TREND"]
    return fs[:MAX_FINDINGS], max(0, len(fs) - MAX_FINDINGS)


def facts_view(a: Assessment) -> Dict:
    note = ""
    if a.early_drift:
        note = "Early signs are present although the vision model's fatigue probability is still low."
    return {
        "risk_level": a.risk_level,
        "fatigue_probability_percent": int(round(a.p_fatigue_smoothed * 100)),
        "trend": a.trend,
        "confidence": a.confidence,
        "early_drift_note": note,
        "findings": [f.plain for f in narrative_findings(a)[0]],
        "data_quality_notes": list(a.quality_notes),
        # Provenance, so the narrator is told what it is looking at. Without this the
        # model has no way to know the numbers were generated rather than measured,
        # and the most fluent possible answer is then also the most misleading.
        # Values are words, never digits, so they cannot widen what the guard
        # accepts as a legitimate number.
        "provenance": {
            "data_class": a.mode_class or "unknown",
            "pipeline": a.mode or "unknown",
            "signal_provenance": _signal_provenance(a.signal_status),
        },
    }


def _fmt(facts: Dict) -> str:
    return "FACTS:\n" + json.dumps(facts, ensure_ascii=True, indent=None)


def build_messages(a: Assessment, violations: Optional[List[str]] = None) -> List[Dict[str, str]]:
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _fmt(_EX1_FACTS)},
        {"role": "assistant", "content": _EX1_OUT},
        {"role": "user", "content": _fmt(_EX2_FACTS)},
        {"role": "assistant", "content": _EX2_OUT},
    ]
    user = _fmt(facts_view(a))
    if violations:
        user += ("\n\nYour previous answer was rejected for: " + "; ".join(violations)
                 + ". Rewrite it using only the facts above and obey every rule.")
    msgs.append({"role": "user", "content": user})
    return msgs
