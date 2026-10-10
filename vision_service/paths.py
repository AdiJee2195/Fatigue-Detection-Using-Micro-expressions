"""Locate the sibling ``xai_narrative`` package.

The ONNX classifier reuses ``fatigue_xai.cv_adapter`` rather than copying its
preprocessing and softmax, so there is exactly one definition of how logits
become probabilities. That reuse needs ``fatigue_xai`` importable.
"""
from __future__ import annotations

import os
import sys

#: Environment override, useful when the deployment layout differs from the repo.
PATH_ENV = "XAI_NARRATIVE_PATH"


def ensure_xai_narrative_importable() -> str:
    """Put the directory containing ``fatigue_xai/`` on ``sys.path`` and return it."""
    if "fatigue_xai" in sys.modules:
        return sys.modules["fatigue_xai"].__path__[0] if hasattr(
            sys.modules["fatigue_xai"], "__path__") else ""

    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.getenv(PATH_ENV, ""),
        os.path.join(os.path.dirname(here), "xai_narrative"),
        os.path.abspath(os.path.join(here, "..", "xai_narrative")),
        "/app/xai_narrative",
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(os.path.join(candidate, "fatigue_xai")):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return candidate
    raise ImportError(
        "could not locate the xai_narrative package (the directory containing "
        f"fatigue_xai/); set {PATH_ENV} to it"
    )
