"""Startup configuration for the narrative XAI service.

Only two things live here today — the bind address — but they live here because
getting them wrong is invisible and expensive.

Why this module exists
----------------------
The service previously did::

    uvicorn.run(..., port=int(os.getenv("PORT", "8100")))

Two failure modes followed from that single expression, both reproduced live:

``PORT=0``
    uvicorn reads 0 as "bind any free port". The gateway connects to a *fixed*
    address (``NARRATIVE_WS_URL=ws://localhost:8100``). A random port is
    indistinguishable from the service being down, so the whole narrative path
    dies and nothing in the logs says why. ``PORT=0`` is exported by some CI
    harnesses and container platforms.

``PORT=abc``
    ``int()`` raised a bare ``ValueError`` traceback pointing at the internals of
    ``uvicorn.run`` — a stack trace where a one-line configuration error belongs.

The vision service already solved this (``vision_service/app.py::resolve_port``)
and its docstring warns about exactly these cases. The fix was simply never
applied here, which is why this module now owns the policy instead of leaving it
inline in an entry point.

Policy
------
* Our own port settings (``XAI_PORT``, then ``PORT``) are **ours to validate**.
  A value that is present but unusable is a configuration error and the process
  fails fast with a readable message. We never silently bind somewhere else.
* Absent means "use the documented default" — not an error.
"""

from __future__ import annotations

import os
import re
from typing import Mapping, Optional

#: The port the gateway's ``NARRATIVE_WS_URL`` points at by default.
DEFAULT_PORT = 8100
DEFAULT_HOST = "0.0.0.0"

#: Preferred setting. ``PORT`` is the fallback because container platforms, and
#: this project's own Dockerfile, inject a generic ``PORT``.
PORT_ENVS = ("XAI_PORT", "PORT")
HOST_ENVS = ("XAI_HOST", "HOST")

#: The full usable TCP port range. 0 is excluded deliberately: it means
#: "allocate anything" to the socket layer, which this service must never do.
MIN_PORT = 1
MAX_PORT = 65535

#: Bare decimal digits only — see :func:`_parse_port` for why this is stricter
#: than ``int()``.
_DIGITS = re.compile(r"^[0-9]+$")


class ConfigError(RuntimeError):
    """A configuration value is present but unusable.

    Raised at startup, never at request time: a misconfigured service should
    refuse to start rather than serve from an address nobody is looking for.
    """


def resolve_port(env: Optional[Mapping[str, str]] = None) -> int:
    """Return the port to bind, or raise :class:`ConfigError`.

    ``XAI_PORT`` wins over ``PORT``; both are validated identically.
    An unset (or blank) setting falls back to :data:`DEFAULT_PORT`.
    """
    source = os.environ if env is None else env

    for name in PORT_ENVS:
        raw = source.get(name)
        if raw is None or not str(raw).strip():
            continue
        value = _parse_port(name, str(raw))
        return value
    return DEFAULT_PORT


def _parse_port(name: str, raw: str) -> int:
    """Validate one setting. Kept separate so the error message names it.

    Only bare decimal digits are accepted. Deliberately stricter than
    ``int()``, which also accepts underscore separators (``8_100``),
    surrounding whitespace, a leading ``+`` and signs. An environment variable
    is not Python source, so a value that looks like a typo should be reported
    rather than quietly reinterpreted as a different port.
    """
    text = raw.strip()
    if not _DIGITS.match(text):
        raise ConfigError(
            f"{name}={text!r} is not a port number. "
            f"Set {name} to a whole number between {MIN_PORT} and {MAX_PORT}, "
            f"or unset it to use the default ({DEFAULT_PORT})."
        )
    value = int(text, 10)

    if value == 0:
        raise ConfigError(
            f"{name}=0 asks the OS to allocate a random port. This service must "
            f"listen on a fixed address, because the gateway connects to it at a "
            f"configured URL (NARRATIVE_WS_URL) and cannot discover a random one. "
            f"Set {name} to a port between {MIN_PORT} and {MAX_PORT}, or unset it "
            f"to use the default ({DEFAULT_PORT})."
        )
    if value > MAX_PORT:
        raise ConfigError(
            f"{name}={value} is outside the usable port range "
            f"{MIN_PORT}-{MAX_PORT}. Unset it to use the default ({DEFAULT_PORT})."
        )
    return value


def resolve_host(env: Optional[Mapping[str, str]] = None) -> str:
    """Return the bind address. ``XAI_HOST`` wins over ``HOST``.

    Host strings are free-form, so the only unusable case is a blank value,
    which is treated as unset rather than as an error.
    """
    source = os.environ if env is None else env
    for name in HOST_ENVS:
        raw = source.get(name)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return DEFAULT_HOST
