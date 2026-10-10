"""Startup configuration: the two port bugs that were reproduced live.

``PORT=0`` used to bind a random port (observed: 52768 instead of 8100) and
``PORT=abc`` used to die with a bare ``ValueError`` pointing into uvicorn. Both
are covered here so neither can come back.
"""
import os
import socket
import subprocess
import sys
import time

import pytest

from fatigue_xai import config
from fatigue_xai.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ConfigError,
    resolve_host,
    resolve_port,
)


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------

def test_unset_ports_fall_back_to_the_documented_default():
    assert resolve_port({}) == DEFAULT_PORT


def test_blank_values_are_treated_as_unset():
    """An empty string in a container env block is "not configured", not an error."""
    assert resolve_port({"PORT": ""}) == DEFAULT_PORT
    assert resolve_port({"XAI_PORT": "   "}) == DEFAULT_PORT


def test_own_setting_wins_over_the_generic_one():
    assert resolve_port({"XAI_PORT": "8101", "PORT": "9001"}) == 8101


def test_generic_port_is_honoured_when_our_setting_is_absent():
    """Container platforms inject PORT; the Dockerfile relies on this."""
    assert resolve_port({"PORT": "9001"}) == 9001


def test_whitespace_around_a_valid_value_is_tolerated():
    assert resolve_port({"PORT": "  8100  "}) == 8100


# ---------------------------------------------------------------------------
# PORT=0 — the reproduced bug
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["XAI_PORT", "PORT"])
def test_zero_is_rejected_instead_of_binding_a_random_port(name):
    """``PORT=0`` must fail fast: the gateway cannot discover a random port.

    This is the regression test for the observed 52768-instead-of-8100 behaviour.
    """
    with pytest.raises(ConfigError) as excinfo:
        resolve_port({name: "0"})
    message = str(excinfo.value)
    assert "random port" in message
    assert name in message, "the error must name the offending setting"
    assert "8100" in message, "the error should point at the default"


# ---------------------------------------------------------------------------
# PORT=abc — the reproduced traceback
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["abc", "80a0", "8_100", "", " ", "8.5", "0x1f", "8100jpeg"])
@pytest.mark.parametrize("name", ["XAI_PORT", "PORT"])
def test_non_numeric_values_raise_a_config_error_not_a_value_error(name, raw):
    """A bad value must produce our message, never a raw ``int()`` traceback.

    ``8_100`` is in this list on purpose: ``int("8_100") == 8100``, so a strict
    check is what stops a typo from silently binding a different port.
    """
    if not raw.strip():
        pytest.skip("blank means unset; covered by the defaults test")
    with pytest.raises(ConfigError) as excinfo:
        resolve_port({name: raw})
    message = str(excinfo.value)
    assert name in message and "not a port number" in message
    # Every rejection points at the escape hatch.
    assert "default (8100)" in message
    # The whole point: the caller can render this, and it does not leak a
    # stack trace from inside uvicorn.
    assert "invalid literal for int()" not in message


@pytest.mark.parametrize("value", [65536, 70000, 999999])
@pytest.mark.parametrize("name", ["XAI_PORT", "PORT"])
def test_too_large_ports_are_rejected(name, value):
    with pytest.raises(ConfigError) as excinfo:
        resolve_port({name: str(value)})
    assert "port range" in str(excinfo.value)


@pytest.mark.parametrize("value", ["-1", "-8100", "+8100"])
@pytest.mark.parametrize("name", ["XAI_PORT", "PORT"])
def test_signed_values_are_rejected_as_non_numeric(name, value):
    """A port is a bare cardinal, so a sign is a typo worth reporting."""
    with pytest.raises(ConfigError) as excinfo:
        resolve_port({name: value})
    assert "not a port number" in str(excinfo.value)


@pytest.mark.parametrize("value", [1, 80, 8100, 65535])
def test_the_boundaries_of_the_usable_range_are_accepted(value):
    assert resolve_port({"PORT": str(value)}) == value


# ---------------------------------------------------------------------------
# host
# ---------------------------------------------------------------------------

def test_host_defaults_to_all_interfaces():
    assert resolve_host({}) == DEFAULT_HOST


def test_own_host_wins_and_blank_falls_through():
    assert resolve_host({"XAI_HOST": "127.0.0.1", "HOST": "0.0.0.0"}) == "127.0.0.1"
    assert resolve_host({"XAI_HOST": "  ", "HOST": "0.0.0.0"}) == "0.0.0.0"


# ---------------------------------------------------------------------------
# the entry point must surface the error, not swallow it
# ---------------------------------------------------------------------------

def _run_entry_point(port: str) -> subprocess.CompletedProcess:
    """Run ``python -m fatigue_xai.api`` with a given PORT, as a real process.

    A subprocess is the only honest way to test this: the bug was in the module's
    ``__main__`` guard, which in-process imports never execute.
    """
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(config.__file__)))
    env = dict(os.environ, PORT=port)
    env.pop("XAI_PORT", None)
    return subprocess.run(
        [sys.executable, "-m", "fatigue_xai.api"],
        cwd=package_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("port", ["abc", "0"])
def test_entry_point_fails_fast_without_a_traceback(port):
    """``python -m fatigue_xai.api`` with a bad port exits 2 with a plain message.

    Exit code 2 (not 1) distinguishes "your configuration is wrong" from "the
    service crashed", and the message must be on stderr with no stack trace.
    """
    result = _run_entry_point(port)
    assert result.returncode == 2, (
        f"expected a clean configuration failure, got exit {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "Traceback" not in result.stderr
    assert "ValueError" not in result.stderr
    assert "PORT=" in result.stderr or "not a port number" in result.stderr or "random port" in result.stderr


def test_entry_point_starts_on_the_configured_port():
    """A valid PORT must actually be the one bound (the inverse of the old bug)."""
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(config.__file__)))
    port = _free_port()
    env = dict(os.environ, PORT=str(port), XAI_HOST="127.0.0.1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "fatigue_xai.api"],
        cwd=package_dir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.time() + 25
        reached = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    reached = True
                    break
            except OSError:
                time.sleep(0.25)
        assert reached, (
            f"service never listened on the configured port {port}; "
            f"exit={proc.poll()}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.wait(timeout=5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_config_error_is_a_runtime_error():
    """So callers that already catch RuntimeError keep working."""
    assert issubclass(ConfigError, RuntimeError)
