"""Narrative generation: LLM (guarded) with deterministic template fallback,
plus the policy that decides *when* a new narrative is worth producing."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional

from .guard import GuardResult, validate
from .llm import LLMClient, ProviderStatus, classify
from .prompt import build_messages, facts_view, narrative_findings
from .schemas import LEVELS, UNKNOWN, Assessment

#: How the narrative currently on screen was produced.  Travels to the browser on
#: every narrative event so the panel can label it without guessing.
SOURCE_LLM = "llm"
SOURCE_TEMPLATE = "template"
SOURCE_FALLBACK = "template_fallback"

#: Why the LLM was or was not used.  ``ok`` and ``guard_rejected`` mean the
#: provider answered; ``unavailable`` means it failed; ``disabled`` means no
#: provider is configured; ``skipped`` means policy chose the template.
LLM_OK = "ok"
LLM_UNAVAILABLE = "unavailable"
LLM_DISABLED = "disabled"
LLM_SKIPPED = "skipped"
LLM_GUARD_REJECTED = "guard_rejected"
LLM_NOT_CALLED = "not_called"

TREND_PHRASE = {"rising": "rising", "falling": "falling", "stable": "stable", "unknown": ""}


def template_body(a: Assessment) -> str:
    """Deterministic rationale built only from the engine's own clauses."""
    if a.risk_level == UNKNOWN:
        return ("The operator's face cannot be assessed reliably right now; " + "; ".join(a.quality_notes) + ".")
    trend = TREND_PHRASE[a.trend]
    head = f"Risk is {a.risk_level}" + (f" and {trend}" if trend else "") + \
           f", with the fatigue probability at {round(a.p_fatigue_smoothed * 100)}%."
    shown, more = narrative_findings(a)
    if shown:
        body = " Observed: " + "; ".join(f.plain for f in shown) + "."
        if more:
            body += " Lower-severity signs are also logged."
    else:
        body = " No individual warning signs were detected."
    extra = ""
    if a.early_drift:
        extra += " Early signs are present although the vision model's fatigue probability is still low."
    if a.quality_notes:
        extra += " Certainty is reduced: " + "; ".join(a.quality_notes) + "."
    return head + body + extra


def compose(a: Assessment, body: str) -> str:
    """Assemble the authoritative narrative text.

    The simulation marker is added here, in code, for two reasons:

    1. It must be present in *every* synthetic narrative — including the
       deterministic template and every guard fallback — not only when a language
       model happens to comply.
    2. It must not be suppressible by prompt injection, a verbose model, or a
       model that simply decides the data looks convincing. The LLM never sees
       the marker as something it must reproduce; the composer owns it.
    """
    lvl = "RISK UNKNOWN" if a.risk_level == UNKNOWN else f"{a.risk_level} RISK"
    head = f"[{a.ts_str}] {lvl} - "
    if a.is_synthetic():
        head += "SIMULATION (synthetic telemetry, not a real driver) - "
    return f"{head}{body.strip()} Recommended action: {a.recommended_action}"


@dataclass
class NarrativeConfig:
    template_only_levels: tuple = ("CRITICAL", UNKNOWN)   # never wait for an LLM for these
    first_token_timeout_s: float = 6.0
    total_timeout_s: float = 12.0
    max_retries: int = 1
    stream_provisional: bool = True
    max_words: int = 80


class NarrativeService:
    def __init__(self, llm: Optional[LLMClient], cfg: Optional[NarrativeConfig] = None):
        self.llm = llm
        self.cfg = cfg or NarrativeConfig()

    async def stream(self, a: Assessment) -> AsyncIterator[Dict]:
        """Yields events: narrative_start, narrative_chunk*, narrative_retry*, narrative_final.

        ``narrative_chunk`` text is PROVISIONAL (unvalidated LLM output).  The
        ``narrative_final`` event carries the authoritative, validated text and
        must replace whatever the UI accumulated.
        """
        nid = uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        #: Which provider would be asked, named for the operator.  ``None`` means the
        #: service is running with no provider at all (NARRATIVE_LLM=none).
        provider = self.provider_name()
        template_only = a.risk_level in self.cfg.template_only_levels
        use_llm = self.llm is not None and not template_only
        base = {"session_id": a.session_id, "narrative_id": nid, "ts": a.ts, "ts_str": a.ts_str,
                "risk_level": a.risk_level, "provider": provider}
        yield {"type": "narrative_start", **base,
               "prefix": compose(a, "").split(" - ")[0] + " - ", "mode": "llm" if use_llm else "template",
               "llm_status": LLM_SKIPPED if template_only else LLM_NOT_CALLED,
               "bypass_reason": "critical_or_unknown_risk" if template_only else None}

        if not use_llm:
            body = template_body(a)
            # A CRITICAL verdict must never wait on a network round trip, so this is
            # a policy decision, not a provider failure.  The two are reported
            # differently ('skipped' vs 'unavailable') so the UI can say which.
            status = LLM_SKIPPED if template_only else LLM_DISABLED
            yield {"type": "narrative_final", **base, "text": compose(a, body), "source": SOURCE_TEMPLATE,
                   "llm_status": status, "error_kind": None,
                   "guard": {"passed": True, "violations": [], "attempts": 0},
                   "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "first_token_ms": None}
            return

        facts = facts_view(a)
        violations: List[str] = []
        first_token_ms: Optional[float] = None
        attempts = 0
        last_viol: List[str] = []
        err_kind: Optional[str] = None
        for attempt in range(self.cfg.max_retries + 1):
            attempts += 1
            msgs = build_messages(a, violations if attempt else None)
            text = ""
            err: Optional[str] = None
            try:
                async for piece in self._timed(self.llm.stream(msgs), t0):
                    if first_token_ms is None:
                        first_token_ms = round((time.perf_counter() - t0) * 1000, 1)
                    text += piece
                    if attempt == 0 and self.cfg.stream_provisional:
                        yield {"type": "narrative_chunk", **base, "text": piece, "provisional": True}
            except (asyncio.TimeoutError, ConnectionError, OSError, RuntimeError, ValueError) as e:  # incl. httpx errors
                err = f"{type(e).__name__}: {e}"
                err_kind = classify(e)
            except Exception as e:  # httpx.HTTPError etc. - never let the LLM take the service down
                err = f"{type(e).__name__}: {e}"
                err_kind = classify(e)
            if err:
                last_viol = [f"llm unavailable ({err})"]
                break
            res: GuardResult = validate(text, facts, max_words=self.cfg.max_words)
            if res.passed:
                yield {"type": "narrative_final", **base, "text": compose(a, res.text), "source": SOURCE_LLM,
                       "llm_status": LLM_OK, "error_kind": None,
                       "guard": {"passed": True, "violations": [], "attempts": attempts},
                       "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "first_token_ms": first_token_ms}
                return
            violations = res.violations
            last_viol = res.violations
            if attempt < self.cfg.max_retries:
                yield {"type": "narrative_retry", **base, "violations": violations}

        # Either the provider failed (err_kind set) or it answered with something the
        # guard would not stand behind.  Both land here, both keep the operator
        # informed via a deterministic explanation, and both say which happened.
        yield {"type": "narrative_final", **base, "text": compose(a, template_body(a)), "source": SOURCE_FALLBACK,
               "llm_status": LLM_UNAVAILABLE if err_kind else LLM_GUARD_REJECTED,
               "error_kind": err_kind,
               "guard": {"passed": False, "violations": last_viol, "attempts": attempts},
               "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "first_token_ms": first_token_ms}

    async def _timed(self, it: AsyncIterator[str], t0: float) -> AsyncIterator[str]:
        """Enforce first-token and total deadlines on an async iterator."""
        ait = it.__aiter__()
        first = True
        try:
            while True:
                remaining = self.cfg.total_timeout_s - (time.perf_counter() - t0)
                limit = min(remaining, self.cfg.first_token_timeout_s) if first else remaining
                if limit <= 0:
                    raise asyncio.TimeoutError("deadline exceeded")
                try:
                    piece = await asyncio.wait_for(ait.__anext__(), timeout=limit)
                except StopAsyncIteration:
                    return
                first = False
                yield piece
        finally:
            aclose = getattr(ait, "aclose", None)
            if aclose:
                await aclose()

    def provider_name(self) -> Optional[str]:
        """The configured provider, as the dashboard should label it.

        ``None`` when the service runs with ``NARRATIVE_LLM=none``: the honest
        answer to "which LLM wrote this?" is then "none", not a blank space the UI
        might fill with a guess.
        """
        return getattr(self.llm, "name", None) if self.llm is not None else None

    def provider_status(self) -> Optional[ProviderStatus]:
        """Observed provider health, or ``None`` when no provider is configured."""
        if self.llm is None:
            return None
        getter = getattr(self.llm, "status", None)
        return getter() if callable(getter) else None

    async def narrate(self, a: Assessment) -> Dict:
        """Non-streaming convenience: returns the final event."""
        final: Dict = {}
        async for ev in self.stream(a):
            if ev["type"] == "narrative_final":
                final = ev
        return final


@dataclass
class NarrationPolicy:
    """Decides when an assessment deserves a (slow, LLM-backed) narrative."""
    repeat_s: float = 30.0          # re-narrate while MODERATE+
    heartbeat_s: float = 300.0      # LOW status line
    min_gap_s: float = 4.0          # debounce between narratives (except escalations)
    deescalation_gap_s: float = 15.0  # don't spam 'risk dropped' messages
    new_finding_gap_s: float = 8.0
    _last_ts: Optional[float] = field(default=None, init=False)
    _last_level: Optional[str] = field(default=None, init=False)
    _last_ids: set = field(default_factory=set, init=False)

    def should_narrate(self, a: Assessment) -> bool:
        if a.calibrating and a.risk_level == "LOW" and not a.findings:
            return False
        ids = set(a.finding_ids())
        gap = float("inf") if self._last_ts is None else a.ts - self._last_ts
        lvl = a.risk_level
        rank = lambda x: LEVELS.index(x) if x in LEVELS else -1  # noqa: E731
        escalation = self._last_level is not None and rank(lvl) > rank(self._last_level)
        changed = lvl != self._last_level

        go = False
        if self._last_level is None:
            go = lvl != "LOW" or a.early_drift
        elif escalation:
            go = True
        elif changed and gap >= (self.min_gap_s if rank(lvl) > rank(self._last_level) else self.deescalation_gap_s):
            go = True
        elif rank(lvl) >= 1 and gap >= self.repeat_s:
            go = True
        elif rank(lvl) >= 1 and (ids - self._last_ids) and gap >= self.new_finding_gap_s:
            go = True
        elif lvl == "LOW" and gap >= self.heartbeat_s:
            go = True
        if go:
            self._last_ts, self._last_level, self._last_ids = a.ts, lvl, ids
        return go
