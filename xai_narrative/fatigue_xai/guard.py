"""Hallucination guard.  Checks LLM text against the facts view it was given.

The guard is deliberately strict and rule-based (no second LLM): anything it
rejects is replaced by the deterministic template narrator, so a wrong claim
can cost a less fluent sentence but never a false statement.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List

from .schemas import LEVELS

_NUM = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")
_AU = re.compile(r"\bAU\s?(\d+)", re.IGNORECASE)
_LEVEL_CTX = re.compile(
    r"\b(low|moderate|high|critical)[- ]risk\b|\brisk\s+(?:level\s+)?(?:is|of|as|at)?\s*(low|moderate|high|critical)\b",
    re.IGNORECASE,
)
_FORBIDDEN = [
    r"\bdiagnos\w*", r"\bsleep apn\w*", r"\bnarcolep\w*", r"\bmedical\w*", r"\bdisease\w*", r"\bdisorder\w*",
    r"\balcohol\w*", r"\bdrunk\b", r"\bintoxicat\w*", r"\bmedicat\w*", r"\bangry\b", r"\bsad\b", r"\banxi\w*",
    r"\bstress\w*", r"\bdepress\w*", r"\bshould\b", r"\bmust\b", r"\bneed(?:s)? to\b", r"\brecommend\w*",
    r"\badvi[sc]\w*", r"\bsuggest\w*", r"\bpull over\b", r"\bstop driving\b", r"\btake a break\b", r"\bplease\b",
    r"\bbecause of\b.*\b(?:night|shift|work|sleep deprivation)\b",
]
_MARKDOWN = re.compile(r"(\*\*|__|^#{1,6}\s|`|^\s*[-*]\s|^\s*\d+\.\s)", re.MULTILINE)
_UNGROUNDED = {"heart": r"\bheart\b|\bpulse\b|\bbpm\b"}

#: Wording that asserts a real person was observed.  Applied ONLY when the facts
#: declare synthetic data, where such a claim is false by construction — a
#: demonstration of the rule engine is not an observation of a driver.  Kept tight
#: so it cannot fire on the impersonal phrasing the findings actually use.
_SYNTHETIC_FORBIDDEN = (
    r"\breal driver\b", r"\blive driver\b", r"\bactual driver\b", r"\bthe driver\b",
    r"\bwe (?:measured|observed|detected)\b", r"\bmeasured (?:live|in real time)\b",
    r"\breal[- ]time measurement\b", r"\breal person\b",
)
_TREND_OPPOSITE = {
    "rising": r"\b(falling|decreas\w*|declin\w*|improv\w*|dropping)\b",
    "falling": r"\b(rising|increas\w*|climb\w*|worsen\w*|growing)\b",
    "stable": r"\b(rising|falling|climb\w*|worsen\w*|improv\w*|declin\w*)\b",
}


@dataclass
class GuardResult:
    passed: bool
    text: str
    violations: List[str] = field(default_factory=list)


_NUM_UNIT = re.compile(
    r"(?<![A-Za-z])(\d+(?:\.\d+)?)(?:(%)|\s*(s|secs?|seconds?)\b|\s*(mins?|minutes?)\b)?", re.IGNORECASE)


def _numbers_with_units(text: str):
    """[(value_str, unit)] where unit is '%', 's', 'min' or '' (plain count/score)."""
    out = []
    for m in _NUM_UNIT.finditer(text):
        unit = "%" if m.group(2) else "s" if m.group(3) else "min" if m.group(4) else ""
        out.append((m.group(1), unit))
    return out


def _allowed_numbers(facts: Dict):
    blob = json.dumps(facts)
    allowed = [(float(n), u) for n, u in _numbers_with_units(blob)]
    # keys such as "fatigue_probability_percent": 72 carry a percentage without a '%' sign
    allowed += [(float(n), "%") for n in re.findall(r'"\w*percent":\s*(\d+(?:\.\d+)?)', blob)]
    return allowed


def _num_ok(n: str, unit: str, allowed) -> bool:
    v = float(n)
    is_int = "." not in n
    for a, u in allowed:
        if u != unit:
            continue
        if is_int:
            if abs(v - a) <= 0.5 and (v == a or not float(a).is_integer()):
                return True
        elif abs(v - a) <= 0.051:
            return True
    return False


def clean(text: str) -> str:
    t = re.sub(r"\s+", " ", text.replace("\r", " ")).strip()
    return t.strip("\"'` ")


def validate(text: str, facts: Dict, max_words: int = 80, min_words: int = 6) -> GuardResult:
    t = clean(text)
    v: List[str] = []
    words = t.split()
    if len(words) < min_words:
        v.append("answer too short")
    if len(words) > max_words:
        v.append(f"answer longer than {max_words} words")
    if len(re.findall(r"[.!?](?:\s|$)", t)) > 5:
        v.append("too many sentences")
    if _MARKDOWN.search(t):
        v.append("markdown or list formatting used")
    if sum(1 for ch in t if ord(ch) > 127) > 0:
        v.append("non-ASCII characters")

    # numbers must come from the facts
    au_stripped = _AU.sub("", t)
    allowed = _allowed_numbers(facts)
    for n, unit in _numbers_with_units(au_stripped):
        if not _num_ok(n, unit, allowed):
            v.append(f"number {n}{unit if unit == '%' else (' ' + unit if unit else '')} not in facts")

    # AU codes must be in the facts
    allowed_au = {m for m in _AU.findall(json.dumps(facts))}
    for m in _AU.findall(t):
        if m not in allowed_au:
            v.append(f"AU{m} not in facts")

    # risk level wording must match
    level = facts.get("risk_level", "")
    for m in _LEVEL_CTX.finditer(t):
        word = (m.group(1) or m.group(2)).upper()
        if word != level:
            v.append(f"states risk level {word} but level is {level}")
    if level in LEVELS and not re.search(rf"\b{level}\b", t, re.IGNORECASE):
        v.append(f"does not state risk level {level}")

    # trend direction
    trend = facts.get("trend", "unknown")
    if trend in _TREND_OPPOSITE and re.search(_TREND_OPPOSITE[trend], t, re.IGNORECASE):
        v.append(f"contradicts trend '{trend}'")

    # forbidden vocabulary
    for pat in _FORBIDDEN:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            v.append(f"forbidden wording '{m.group(0)}'")

    # signals not provided
    blob = json.dumps(facts).lower()
    for key, pat in _UNGROUNDED.items():
        if key not in blob and re.search(pat, t, re.IGNORECASE):
            v.append("mentions a signal not in facts")

    # Synthetic inputs may not be reported as an observation of a person.
    #
    # Only the *harm* is enforced here. Labelling the output as simulated is not
    # this check's job: ``compose()`` prepends that marker deterministically, so a
    # model that forgets to mention it still cannot produce an unlabelled
    # narrative. Requiring the word here as well would reject perfectly good
    # writing in demo mode — the mode most reviewers will actually run — for no
    # additional safety.
    provenance = facts.get("provenance") or {}
    if str(provenance.get("data_class", "")).lower() == "synthetic":
        for pat in _SYNTHETIC_FORBIDDEN:
            m = re.search(pat, t, re.IGNORECASE)
            if m:
                v.append(
                    f"describes synthetic data as a real observation ('{m.group(0)}')"
                )

    # every finding must be reflected (by its AU code or one of its numbers)
    text_au = set(_AU.findall(t))
    text_nums = {float(x) for x in _NUM.findall(au_stripped)}
    for f in facts.get("findings", []):
        codes = set(_AU.findall(f))
        nums = {float(x) for x in _NUM.findall(_AU.sub("", f))}
        if not codes and not nums:
            continue
        hit = bool(codes & text_au) or any(abs(n - m) <= 0.5 for n in nums for m in text_nums)
        if not hit:
            v.append("omits a finding: " + f[:50])

    # dedupe, keep order
    seen, out = set(), []
    for x in v:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return GuardResult(passed=not out, text=t, violations=out)
