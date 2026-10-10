"""Telemetry contract between the CV stage and the narrative (XAI) stage.

One FrameTelemetry per processed video frame.  Only ``ts`` and ``class_probs``
are mandatory (that is all the current EfficientNet model provides); EAR, FACS
Action-Unit intensities and rPPG are optional so the rule engine degrades
gracefully until the EVM / optical-flow stage is wired in.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Current model (handover): 0=microsleep 1=neutral 2=yawning
LABELS_3 = ("microsleep", "neutral", "yawning")
# Proposed schema (handover, future work #1)
LABELS_4 = ("alert", "early_fatigue", "yawning", "microsleep")
KNOWN_LABELS = frozenset(LABELS_3) | frozenset(LABELS_4)
NON_FATIGUE_LABELS = frozenset({"neutral", "alert"})

LEVELS = ("LOW", "MODERATE", "HIGH", "CRITICAL")
UNKNOWN = "UNKNOWN"

#: Pipeline identity, mirroring ``vision_service.telemetry``: which code path
#: produced a row.  Kept in step with that module by the contract tests.
MODE_STUB = "stub"
MODE_ONNX = "onnx"

#: Canonical provenance vocabulary shared by the vision service, the gateway and
#: the dashboard.  ``synthetic`` means the numbers were generated, not measured.
MODE_CLASS_SYNTHETIC = "synthetic"
MODE_CLASS_LIVE = "live"
MODE_CLASS_UNKNOWN = "unknown"
MODE_CLASSES = frozenset({MODE_CLASS_SYNTHETIC, MODE_CLASS_LIVE, MODE_CLASS_UNKNOWN})

_AU_KEY = re.compile(r"^\s*(?:au)?\s*0*(\d+)\s*$", re.IGNORECASE)


class FrameTelemetry(BaseModel):
    # The gateway sends the session id from the WebSocket URL and may add fields
    # we do not model yet (e.g. sessionId); ignore them instead of rejecting the
    # frame. Only ts + class_probs are mandatory.
    model_config = ConfigDict(extra="ignore")

    session_id: str = "default"
    frame_id: int = 0
    ts: float = Field(..., description="Seconds. Epoch time or session-relative.")
    class_probs: Dict[str, float]
    p_fatigue: Optional[float] = Field(None, ge=0.0, le=1.0)
    ear: Optional[float] = Field(None, ge=0.0, le=1.0, description="Eye Aspect Ratio")
    au: Dict[str, float] = Field(default_factory=dict, description="FACS AU intensity 0-5, e.g. {'AU4': 2.1}")
    rppg_bpm: Optional[float] = Field(None, ge=20, le=250)
    face_detected: bool = True
    illumination: Optional[float] = Field(None, ge=0.0, le=1.0, description="Mean face-ROI luminance 0-1")

    # ---- provenance -------------------------------------------------------
    # Without these two fields the rule engine cannot tell a synthetic
    # demonstration from an observation of a real person, and neither can the LLM
    # it briefs. They are optional because a producer that does not know must be
    # able to say so by omitting them, which is reported as "unknown" rather than
    # silently defaulted to "live".
    mode: Optional[str] = Field(None, description="pipeline identity: 'stub' | 'onnx'")
    mode_class: Optional[str] = Field(
        None, description="canonical provenance: 'synthetic' | 'live' | 'unknown'")
    signal_status: Optional[Dict[str, str]] = Field(
        None, description="per-metric provenance, e.g. {'ear': 'live', 'rppg_bpm': 'not_configured'}")

    @field_validator("mode_class", mode="before")
    @classmethod
    def _norm_mode_class(cls, v: Any) -> Optional[str]:
        """Accept only the canonical vocabulary; anything else becomes 'unknown'.

        Normalising rather than rejecting: a newer producer sending a value this
        version does not know must degrade into "we cannot vouch for this", never
        into a validation error that silently drops the frame.
        """
        if v is None:
            return None
        text = str(v).strip().lower()
        return text if text in MODE_CLASSES else MODE_CLASS_UNKNOWN

    def provenance_is_synthetic(self) -> bool:
        """True only when the producer explicitly declared synthetic data.

        Both signals are honoured: the canonical ``mode_class`` when present, and
        otherwise the legacy ``mode`` string, so a gateway that has not been
        upgraded yet still yields the honest answer.
        """
        if self.mode_class:
            return self.mode_class == MODE_CLASS_SYNTHETIC
        return (self.mode or "").strip().lower() == "stub"

    @field_validator("au", mode="before")
    @classmethod
    def _norm_au(cls, v: Any) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for k, val in (v or {}).items():
            m = _AU_KEY.match(str(k))
            if not m:
                raise ValueError(f"bad AU key {k!r}; expected like 'AU4'")
            out[f"AU{int(m.group(1))}"] = min(5.0, max(0.0, float(val)))
        return out

    @field_validator("class_probs", mode="before")
    @classmethod
    def _norm_probs(cls, v: Any) -> Dict[str, float]:
        if not v:
            raise ValueError("class_probs is required")
        probs = {str(k).lower(): float(p) for k, p in v.items()}
        unknown = set(probs) - KNOWN_LABELS
        if unknown:
            raise ValueError(f"unknown class labels: {sorted(unknown)}")
        total = sum(probs.values())
        if not 0.95 <= total <= 1.05:
            raise ValueError(f"class_probs must sum to ~1 (got {total:.3f})")
        return {k: min(1.0, max(0.0, p / total)) for k, p in probs.items()}

    @model_validator(mode="after")
    def _derive_p_fatigue(self) -> "FrameTelemetry":
        if self.p_fatigue is None:
            self.p_fatigue = float(
                min(1.0, sum(p for k, p in self.class_probs.items() if k not in NON_FATIGUE_LABELS))
            )
        return self

    def prob(self, label: str) -> float:
        return self.class_probs.get(label, 0.0)

    def au_value(self, code: str) -> Optional[float]:
        return self.au.get(code)


class Finding(BaseModel):
    id: str
    label: str
    facs: List[str] = Field(default_factory=list)
    severity: int = Field(..., ge=1, le=3)
    evidence: Dict[str, float] = Field(default_factory=dict)
    plain: str = Field(..., description="One plain-language clause; the ONLY wording the LLM may use for this finding.")
    pre_attentive: bool = False


class Assessment(BaseModel):
    session_id: str
    ts: float
    ts_str: str
    window_s: float
    risk_level: str
    raw_level: str
    p_fatigue_now: float
    p_fatigue_smoothed: float
    trend: str  # rising | falling | stable | unknown
    trend_slope_per_min: Optional[float] = None
    confidence: str  # high | medium | low
    early_drift: bool = False
    findings: List[Finding] = Field(default_factory=list)
    quality_notes: List[str] = Field(default_factory=list)
    reliable: bool = True
    calibrating: bool = False
    recommended_action: str = ""
    signals_available: List[str] = Field(default_factory=list)

    #: Provenance of the telemetry this verdict was computed from. Copied through
    #: from the frames, never inferred here: an assessment that cannot say whether
    #: its inputs were measured cannot be reported honestly.
    mode: Optional[str] = None
    mode_class: Optional[str] = None
    signal_status: Dict[str, str] = Field(default_factory=dict)

    def is_synthetic(self) -> bool:
        return self.mode_class == MODE_CLASS_SYNTHETIC

    def provenance_summary(self) -> str:
        """One clause for the narrative, or an empty string when live/unknown.

        Code, not prose: the simulation marker is added deterministically so it
        cannot be dropped by a model that decides to be more confident than its
        inputs allow.
        """
        if not self.is_synthetic():
            return ""
        return ("SIMULATION: these readings are synthetic and do not describe a real "
                "driver; the risk level is the configured rule engine's response to "
                "generated input.")

    def finding_ids(self) -> List[str]:
        return [f.id for f in self.findings]


def format_ts(ts: float) -> str:
    """Epoch -> local HH:MM:SS; small values -> session-relative T+HH:MM:SS."""
    if ts > 1e9:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    s = int(max(0, ts))
    return f"T+{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
