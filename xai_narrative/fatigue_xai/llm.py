"""LLM backends.  All stream plain-text chunks.  Decoding is pinned to be as
deterministic as the backend allows (temperature 0, top-k 1, fixed seed).

Providers
---------
``none``    no provider (deterministic template narratives only)
``groq``    Groq's OpenAI-compatible API (https://console.groq.com)
``openai``  any OpenAI-compatible ``/v1/chat/completions`` (vLLM, llama.cpp)
``ollama``  a local Ollama server

The narrative service never depends on any of these being reachable.  A provider
that is unconfigured, down, rate-limited or slow produces a classified
:class:`LLMError`; the caller falls back to the deterministic template.  That is
why a missing API key is a *degraded* state and not a startup failure — see
``llm_from_env``.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol

import httpx

from .config import ConfigError

Messages = List[Dict[str, str]]


# ---------------------------------------------------------------------------
# error taxonomy
# ---------------------------------------------------------------------------

#: Every failure mode a provider can report.  Kept as plain strings (not an enum)
#: because they travel to the browser inside the narrative event payload and are
#: compared against a TypeScript union there.
class ErrorKind:
    NOT_CONFIGURED = "not_configured"
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "model_not_found"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER_ERROR = "server_error"
    MALFORMED_RESPONSE = "malformed_response"
    EMPTY_RESPONSE = "empty_response"


class LLMError(RuntimeError):
    """A provider call failed, with a classified reason.

    ``retryable`` records whether a *bounded* retry could plausibly succeed.  It is
    deliberately False for every 4xx: a bad key, a bad model name or a rejected
    request will fail identically on the next attempt, and retrying only delays the
    deterministic fallback and burns quota.
    """

    def __init__(self, kind: str, message: str, *, status: Optional[int] = None,
                 retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retryable = retryable


def classify(exc: BaseException) -> str:
    """Best-effort error kind for *any* exception, for providers that raise raw
    httpx errors.  Explicit :class:`LLMError` kinds are always preferred."""
    if isinstance(exc, LLMError):
        return exc.kind
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return ErrorKind.TIMEOUT
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, ConnectionError, OSError)):
        return ErrorKind.CONNECTION
    if isinstance(exc, httpx.HTTPStatusError):
        return kind_from_status(exc.response.status_code)
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return ErrorKind.MALFORMED_RESPONSE
    return type(exc).__name__


def kind_from_status(status: int) -> str:
    """Map an HTTP status onto the taxonomy."""
    if status == 401:
        return ErrorKind.UNAUTHORIZED
    if status == 403:
        return ErrorKind.FORBIDDEN
    if status == 404:
        return ErrorKind.NOT_FOUND
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if 400 <= status < 500:
        return ErrorKind.BAD_REQUEST
    if status >= 500:
        return ErrorKind.SERVER_ERROR
    return ErrorKind.BAD_REQUEST


#: 4xx responses are deterministic: the same request will fail the same way.
_RETRYABLE_KINDS = frozenset({ErrorKind.CONNECTION, ErrorKind.TIMEOUT,
                              ErrorKind.SERVER_ERROR, ErrorKind.RATE_LIMITED})


def redact(text: str, secret: Optional[str]) -> str:
    """Remove a credential from a string before it is logged or returned.

    Providers echo request details in error bodies.  Nothing that reaches the
    logs, ``/health`` or the browser may contain the key, so every provider
    message is passed through here.
    """
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


@dataclass
class ProviderStatus:
    """Observable provider state, for ``/health`` and the dashboard.

    Counters are per-process and in-memory, matching the rest of the service.
    """

    enabled: bool = False
    model: Optional[str] = None
    reason: Optional[str] = None
    calls: int = 0
    failures: int = 0
    last_error_kind: Optional[str] = None
    last_error_at: Optional[float] = None
    last_error_message: Optional[str] = None
    last_ok_at: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "reason": self.reason,
            "calls": self.calls,
            "failures": self.failures,
            "last_error_kind": self.last_error_kind,
            "last_error_at": self.last_error_at,
            "last_error_message": self.last_error_message,
            "last_ok_at": self.last_ok_at,
        }


class LLMClient(Protocol):
    name: str

    def stream(self, messages: Messages) -> AsyncIterator[str]: ...

    def status(self) -> ProviderStatus: ...


class _Tracked:
    """Mixin: in-memory call counters plus a classified last-error record.

    Attached to every provider so the dashboard can answer "is the LLM working?"
    with observed facts rather than a guess. State is created lazily, which keeps
    the existing providers' constructors unchanged.
    """

    name: str = "unknown"
    model: Optional[str] = None

    def _status_ref(self) -> ProviderStatus:
        existing = getattr(self, "_provider_status", None)
        if existing is None:
            existing = ProviderStatus(
                enabled=bool(getattr(self, "configured", True)),
                model=getattr(self, "model", None),
            )
            self._provider_status = existing  # type: ignore[attr-defined]
        return existing

    def status(self) -> ProviderStatus:
        return self._status_ref()

    def _record_call(self) -> None:
        self._status_ref().calls += 1

    def _record_ok(self) -> None:
        st = self._status_ref()
        st.last_ok_at = time.time()
        st.last_error_kind = None
        st.last_error_message = None

    def _record_failure(self, exc: BaseException) -> None:
        st = self._status_ref()
        st.failures += 1
        st.last_error_kind = classify(exc)
        st.last_error_at = time.time()
        st.last_error_message = redact(str(exc), getattr(self, "api_key", None))[:300]


class OllamaClient(_Tracked):
    """Local Ollama server (https://ollama.com).  Default model: Llama 3.1 8B."""

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None,
                 seed: int = 42, num_predict: int = 140, keep_alive: str = "30m", transport=None):
        self.base_url = (base_url or os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.1:8b")
        self.seed, self.num_predict, self.keep_alive = seed, num_predict, keep_alive
        self._transport = transport
        self.name = f"ollama:{self.model}"

    async def stream(self, messages: Messages) -> AsyncIterator[str]:
        payload = {
            "model": self.model, "messages": messages, "stream": True, "keep_alive": self.keep_alive,
            "options": {"temperature": 0, "top_k": 1, "top_p": 1, "seed": self.seed,
                        "num_predict": self.num_predict, "num_ctx": 2048},
        }
        timeout = httpx.Timeout(connect=2.0, read=30.0, write=5.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    if "error" in obj:
                        raise RuntimeError(obj["error"])
                    piece = (obj.get("message") or {}).get("content", "")
                    if piece:
                        yield piece
                    if obj.get("done"):
                        return

    async def is_up(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.5, transport=self._transport) as c:
                r = await c.get(f"{self.base_url}/api/tags")
                return r.status_code == 200
        except Exception:
            return False


class OpenAICompatClient(_Tracked):
    """vLLM / llama.cpp server / any OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None,
                 api_key: Optional[str] = None, seed: int = 42, max_tokens: int = 140, transport=None):
        self.base_url = (base_url or os.getenv("OPENAI_COMPAT_URL", "http://localhost:8000/v1")).rstrip("/")
        self.model = model or os.getenv("OPENAI_COMPAT_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct")
        self.api_key = api_key or os.getenv("OPENAI_COMPAT_KEY", "EMPTY")
        self.seed, self.max_tokens = seed, max_tokens
        self._transport = transport
        self.name = f"openai-compat:{self.model}"

    async def stream(self, messages: Messages) -> AsyncIterator[str]:
        payload = {"model": self.model, "messages": messages, "stream": True, "temperature": 0,
                   "seed": self.seed, "max_tokens": self.max_tokens}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        timeout = httpx.Timeout(connect=2.0, read=30.0, write=5.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
            async with client.stream("POST", f"{self.base_url}/chat/completions", json=payload, headers=headers) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    delta = json.loads(data)["choices"][0].get("delta", {})
                    if delta.get("content"):
                        yield delta["content"]


#: Groq's OpenAI-compatible base URL.  A default, not a credential, so it may live
#: in source.  The key and the model name may not.
GROQ_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_DEFAULT_TIMEOUT_S = 8.0
GROQ_DEFAULT_MAX_TOKENS = 200


def _positive_number(name: str, raw: Optional[str], default: float) -> float:
    """Validate a numeric setting, failing fast with a readable message.

    A malformed timeout is a configuration error, not an outage: silently using
    the default would hide a typo in the very setting that bounds latency.
    """
    if raw is None or not str(raw).strip():
        return default
    text = str(raw).strip()
    try:
        value = float(text)
    except ValueError:
        raise ConfigError(
            f"{name}={text!r} is not a number. Set {name} to a positive number, "
            f"or unset it to use the default ({default:g})."
        ) from None
    if value <= 0 or value != value or value in (float("inf"), float("-inf")):
        raise ConfigError(
            f"{name}={value:g} must be greater than zero. Unset it to use the "
            f"default ({default:g})."
        )
    return value


class ScriptedLLM(_Tracked):
    """Test double: returns canned answers in order (last one repeats)."""

    def __init__(self, answers: List[str], chunk_words: int = 3, delay_s: float = 0.0, fail: bool = False):
        self.answers, self.chunk_words, self.delay, self.fail = answers, chunk_words, delay_s, fail
        self.calls: List[Messages] = []
        self.name = "scripted"

    async def stream(self, messages: Messages) -> AsyncIterator[str]:
        self.calls.append(messages)
        if self.fail:
            raise ConnectionError("scripted failure")
        text = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        words = text.split(" ")
        for i in range(0, len(words), self.chunk_words):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield " ".join(words[i:i + self.chunk_words]) + (" " if i + self.chunk_words < len(words) else "")


class GroqClient(_Tracked):
    """Groq's OpenAI-compatible chat-completions API.

    Configuration (all server-side; the key never reaches the browser):

    ===========================  =================================================
    ``GROQ_API_KEY``             required to be *enabled*; absent means the
                                 narrative falls back to the template
    ``GROQ_MODEL``               required to be enabled; no model is hard-coded,
                                 because the catalogue changes
    ``GROQ_BASE_URL``            default :data:`GROQ_DEFAULT_BASE_URL`
    ``GROQ_TIMEOUT_SECONDS``     response deadline, default 8
    ``GROQ_MAX_TOKENS``          output cap, default 200
    ===========================  =================================================

    An unconfigured client is not an error: :meth:`is_configured` is False, every
    :meth:`stream` raises ``LLMError('not_configured')``, and the service keeps
    serving deterministic narratives.  That is the difference between "Groq is not
    set up" and "Groq is down", and neither may stop a monitoring session.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 base_url: Optional[str] = None, timeout_s: Optional[float] = None,
                 max_tokens: Optional[int] = None, seed: int = 42, transport=None,
                 max_attempts: int = 2):
        self.api_key = api_key if api_key is not None else os.getenv("GROQ_API_KEY", "")
        self.model = model if model is not None else os.getenv("GROQ_MODEL")
        self.base_url = (base_url or os.getenv("GROQ_BASE_URL") or GROQ_DEFAULT_BASE_URL).rstrip("/")
        self.timeout_s = float(
            timeout_s if timeout_s is not None
            else _positive_number("GROQ_TIMEOUT_SECONDS", os.getenv("GROQ_TIMEOUT_SECONDS"),
                                  GROQ_DEFAULT_TIMEOUT_S)
        )
        raw_tokens = max_tokens if max_tokens is not None else _positive_number(
            "GROQ_MAX_TOKENS", os.getenv("GROQ_MAX_TOKENS"), GROQ_DEFAULT_MAX_TOKENS)
        self.max_tokens = int(raw_tokens)
        self.seed = seed
        self._transport = transport
        #: Bounded: one retry is enough to ride out a dropped connection, and more
        #: would multiply a provider incident by the number of open sessions.
        self.max_attempts = max(1, int(max_attempts))
        self.name = f"groq:{self.model}" if self.model else "groq"

    # -- configuration ----------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model)

    def is_configured(self) -> bool:
        return self.configured

    def config_problem(self) -> Optional[str]:
        """Why the client is disabled, in operator-facing words (never the key)."""
        if self.configured:
            return None
        missing = []
        if not self.api_key:
            missing.append("GROQ_API_KEY")
        if not self.model:
            missing.append("GROQ_MODEL")
        return "not configured: " + ", ".join(missing) + " not set"

    def status(self) -> ProviderStatus:
        st = self._status_ref()
        st.enabled = self.configured
        st.model = self.model
        st.reason = self.config_problem()
        return st

    # -- request ----------------------------------------------------------

    async def stream(self, messages: Messages) -> AsyncIterator[str]:
        if not self.configured:
            raise LLMError(ErrorKind.NOT_CONFIGURED, self.config_problem() or "not configured")

        self._record_call()
        attempt = 0
        while True:
            attempt += 1
            emitted = False
            try:
                async for piece in self._attempt(messages):
                    emitted = True
                    yield piece
                self._record_ok()
                return
            except LLMError as e:
                self._record_failure(e)
                # Retry only if the failure is plausibly transient AND no text has
                # been streamed yet.  Retrying after partial output would restart
                # the sentence and duplicate words in the final narrative.
                if e.retryable and not emitted and attempt < self.max_attempts:
                    await asyncio.sleep(0.25 * attempt)
                    continue
                raise
            except Exception as e:  # noqa: BLE001 - classify, never leak raw
                self._record_failure(e)
                # Belt and braces: any provider exception text is redacted on the
                # way out, so nothing unredacted can reach the caller.
                wrapped = LLMError(classify(e), redact(str(e), self.api_key))
                if not emitted and attempt < self.max_attempts and classify(e) in _RETRYABLE_KINDS:
                    await asyncio.sleep(0.25 * attempt)
                    continue
                raise wrapped from e

    async def _attempt(self, messages: Messages) -> AsyncIterator[str]:
        """One HTTP request. All provider failures leave here as LLMError."""
        payload = {
            "model": self.model, "messages": messages, "stream": True,
            "temperature": 0, "seed": self.seed, "max_tokens": self.max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        timeout = httpx.Timeout(connect=min(2.0, self.timeout_s), read=self.timeout_s,
                                write=min(5.0, self.timeout_s), pool=min(5.0, self.timeout_s))
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                async with client.stream("POST", f"{self.base_url}/chat/completions",
                                         json=payload, headers=headers) as r:
                    if r.status_code >= 400:
                        detail = await _error_detail(r)
                        # Redact HERE, not only when recording status. This message
                        # travels: it becomes a guard violation, which is relayed to
                        # the browser inside narrative_final. Providers habitually
                        # echo the request (including the Authorization header) in
                        # an auth error, so an unredacted body is a key leak to the
                        # client, not just to the log.
                        raise LLMError(
                            kind_from_status(r.status_code),
                            redact(f"Groq returned HTTP {r.status_code}: {detail}", self.api_key),
                            status=r.status_code,
                            retryable=kind_from_status(r.status_code) in _RETRYABLE_KINDS,
                        )
                    any_text = False
                    async for line in r.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        chunk = _parse_sse_chunk(data)
                        if chunk:
                            any_text = True
                            yield chunk
                    if not any_text:
                        # Observed live with a reasoning model (Groq's gpt-oss family):
                        # the chain of thought streams on a separate `reasoning`
                        # channel that this parser ignores on purpose, yet those
                        # tokens still count against max_tokens. A reply that runs
                        # out of budget mid-reasoning therefore contains no text at
                        # all, and the operator needs to be told which knob to turn.
                        raise LLMError(
                            ErrorKind.EMPTY_RESPONSE,
                            "Groq returned no text content; a reasoning model may need "
                            f"a larger GROQ_MAX_TOKENS (currently {self.max_tokens})")
        except LLMError:
            raise
        except httpx.TimeoutException as e:
            raise LLMError(ErrorKind.TIMEOUT,
                           f"Groq did not respond within {self.timeout_s:g}s",
                           retryable=True) from e
        except (httpx.ConnectError, httpx.NetworkError) as e:
            raise LLMError(ErrorKind.CONNECTION,
                           f"cannot reach Groq at {self.base_url}: {type(e).__name__}",
                           retryable=True) from e
        except httpx.HTTPStatusError as e:  # raised by raise_for_status paths
            status = e.response.status_code if e.response is not None else None
            kind = kind_from_status(status) if status else ErrorKind.CONNECTION
            raise LLMError(kind, redact(f"Groq returned HTTP {status}", self.api_key),
                           status=status, retryable=kind in _RETRYABLE_KINDS) from e

    async def is_up(self) -> Optional[bool]:
        """Cheap reachability probe.  ``None`` = not configured, so nothing to ask.

        A negative answer is *classified and recorded*, not merely returned as
        False. "Groq is down" and "your key was rejected" are the same boolean and
        completely different fixes, and a smoke test that cannot tell them apart
        sends the operator looking in the wrong place.
        """
        if not self.configured:
            return None
        try:
            async with httpx.AsyncClient(timeout=5.0, transport=self._transport) as c:
                r = await c.get(f"{self.base_url}/models",
                                headers={"Authorization": f"Bearer {self.api_key}"})
                if r.status_code == 200:
                    return True
                kind = kind_from_status(r.status_code)
                self._record_failure(LLMError(
                    kind, redact(f"probe returned HTTP {r.status_code}", self.api_key),
                    status=r.status_code))
                return False
        except httpx.TimeoutException as e:
            self._record_failure(LLMError(ErrorKind.TIMEOUT, "probe timed out"))
            return False
        except Exception as e:  # noqa: BLE001 - never leak a raw transport error
            self._record_failure(e)
            return False


async def _error_detail(response: httpx.Response) -> str:
    """Best-effort human detail from an error body, truncated and safe.

    Groq's OpenAI-compatible errors are JSON with an ``error.message``.  Anything
    else (HTML, a proxy page) is reported by status only, never echoed wholesale.
    """
    try:
        raw = await response.aread()
    except Exception:
        return "no body"
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])[:200]
            if isinstance(err, str):
                return err[:200]
            if body.get("message"):
                return str(body["message"])[:200]
    except Exception:
        pass
    return f"unparseable body ({len(raw)} bytes)"


def _parse_sse_chunk(data: str) -> str:
    """Extract the delta text from one SSE ``data:`` line.

    A malformed chunk raises :class:`LLMError` rather than ``IndexError`` so the
    caller's classification stays meaningful.
    """
    try:
        obj = json.loads(data)
        return obj["choices"][0].get("delta", {}).get("content") or ""
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        raise LLMError(ErrorKind.MALFORMED_RESPONSE,
                       f"unreadable Groq stream chunk: {type(e).__name__}") from e


def llm_from_env() -> Optional[LLMClient]:
    """NARRATIVE_LLM = none | groq | openai | ollama.

    ``none`` is the default in this repository's ``.env.example`` and Compose file,
    so a fresh clone needs no key, no model and no network.  ``groq`` with a missing
    key or model still returns a client: it reports ``enabled: false`` from
    ``/health`` and the narrator uses the deterministic template, which is the
    documented degradation rather than a startup failure.
    """
    kind = os.getenv("NARRATIVE_LLM", "ollama").strip().lower()
    if kind in ("none", "", "off", "disabled"):
        return None
    if kind == "groq":
        return GroqClient()
    if kind == "openai":
        return OpenAICompatClient()
    if kind == "ollama":
        return OllamaClient()
    raise ConfigError(
        f"NARRATIVE_LLM={kind!r} is not a known provider. "
        "Use one of: none, groq, openai, ollama."
    )
