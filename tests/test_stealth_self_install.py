"""Stealth-escalation dependency ownership: plugin-owned self-install.

The plugin must not depend on core's ``tools.lazy_deps`` allowlist for its
scrapling stealth leg (that allowlist is reserved for in-core backends).
These tests pin ``_resolve_scrapling_fetcher``'s contract: exact pip argv
and subprocess kwargs, sanitized child environment, single-flight under
concurrent callers, failure caching for the process lifetime, soft
degradation on broken installs and missing pip, and kill-switch casing.
No test may reach a real pip process.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import types

import pytest

import hermes_web_crawl4ai.provider as provider
from hermes_web_crawl4ai.provider import _redact, _resolve_scrapling_fetcher


class _FakeFetcher:
    """Stands in for scrapling.StealthyFetcher in sys.modules."""

    marker = "fake-stealthyfetcher"


class _Proc:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


def _install_fake_scrapling_module(monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = types.ModuleType("scrapling")
    pkg.StealthyFetcher = _FakeFetcher
    monkeypatch.setitem(sys.modules, "scrapling", pkg)


def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test a fresh resolution state and no scrapling module."""
    monkeypatch.setattr(provider, "_scrapling_state", {"resolved": False, "fetcher": None})
    monkeypatch.delitem(sys.modules, "scrapling", raising=False)
    monkeypatch.delenv(provider._SCRAPLING_INSTALL_ENV, raising=False)


def _forbid_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_run(*args, **kwargs):  # noqa: ANN002, ANN003
        pytest.fail("test attempted a real subprocess.run")

    monkeypatch.setattr(subprocess, "run", _fail_run)


class _PipRecorder:
    """Records subprocess.run calls; optionally makes scrapling importable on success."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        returncode: int = 0,
        fix_imports: bool = True,
    ) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self._monkeypatch = monkeypatch
        self._returncode = returncode
        self._fix_imports = fix_imports
        monkeypatch.setattr(subprocess, "run", self)

    def __call__(self, argv, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append((list(argv), kwargs))
        if self._returncode == 0 and self._fix_imports:
            _install_fake_scrapling_module(self._monkeypatch)
        return _Proc(returncode=self._returncode, stderr="simulated failure")


def test_fetcher_returned_when_scrapling_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_state(monkeypatch)
    _install_fake_scrapling_module(monkeypatch)
    _forbid_subprocess(monkeypatch)
    assert _resolve_scrapling_fetcher() is _FakeFetcher


def test_pip_argv_and_kwargs_are_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_state(monkeypatch)
    rec = _PipRecorder(monkeypatch)
    fetcher = _resolve_scrapling_fetcher()
    assert fetcher is _FakeFetcher
    assert len(rec.calls) == 1
    argv, kwargs = rec.calls[0]
    # Exact argv: interpreter, module form, isolated, non-interactive, pinned spec.
    assert argv == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--isolated",
        "--no-input",
        "--quiet",
        provider._SCRAPLING_SPEC,
    ]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == provider._SCRAPLING_INSTALL_TIMEOUT_SECONDS
    assert kwargs["stdin"] == subprocess.DEVNULL
    # Child environment must not inherit host pip/python overrides; the only
    # PIP_ keys present are the two we deliberately set.
    child_env = kwargs["env"]
    inherited_pip = {k for k in child_env if k.upper().startswith("PIP_")} - {
        "PIP_NO_INPUT",
        "PIP_DISABLE_PIP_VERSION_CHECK",
    }
    assert not inherited_pip
    assert child_env["PIP_NO_INPUT"] == "1"
    assert not any(k.upper() in ("PYTHONPATH", "PYTHONHOME") for k in child_env)


def test_child_env_strips_hostile_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIP_INDEX_URL", "https://evil.example.com/simple")
    monkeypatch.setenv("PYTHONPATH", "/tmp/somewhere")
    env = provider._pip_env()
    assert "PIP_INDEX_URL" not in env
    assert "PYTHONPATH" not in env
    assert env["PIP_NO_INPUT"] == "1"


def test_concurrent_first_calls_run_pip_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_state(monkeypatch)
    rec = _PipRecorder(monkeypatch)
    results: list[object] = []
    barrier = threading.Barrier(4)

    def _worker() -> None:
        barrier.wait()  # maximize contention on the first call
        results.append(_resolve_scrapling_fetcher())

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(rec.calls) == 1  # single-flight
    assert all(r is _FakeFetcher for r in results)


def test_failed_install_is_cached_for_process_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_state(monkeypatch)
    _PipRecorder(monkeypatch, returncode=1)
    assert _resolve_scrapling_fetcher() is None
    # Second call must not retry pip.
    _forbid_subprocess(monkeypatch)
    assert _resolve_scrapling_fetcher() is None


def test_no_install_switch_skips_pip_in_any_casing(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("1", "TRUE", "Yes"):
        _reset_state(monkeypatch)
        monkeypatch.setenv(provider._SCRAPLING_INSTALL_ENV, value)
        _forbid_subprocess(monkeypatch)
        assert _resolve_scrapling_fetcher() is None, value


def test_no_install_switch_still_uses_present_scrapling(monkeypatch: pytest.MonkeyPatch) -> None:
    # The switch forbids installing, not stealth itself.
    _reset_state(monkeypatch)
    _install_fake_scrapling_module(monkeypatch)
    monkeypatch.setenv(provider._SCRAPLING_INSTALL_ENV, "1")
    _forbid_subprocess(monkeypatch)
    assert _resolve_scrapling_fetcher() is _FakeFetcher


def test_broken_install_degrades_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    # A package that is present but raises during attribute resolution: one
    # repair install is attempted, the package stays broken, the outcome is
    # cached as unavailable — and nothing escapes as a hard error.
    _reset_state(monkeypatch)
    pkg = types.ModuleType("scrapling")

    def _getattr(name):
        raise RuntimeError("partially broken install")

    pkg.__getattr__ = _getattr  # type: ignore[method-assign]
    monkeypatch.setitem(sys.modules, "scrapling", pkg)
    # pip "succeeds" but the package stays broken (fix_imports=False)
    rec = _PipRecorder(monkeypatch, returncode=0, fix_imports=False)
    assert _resolve_scrapling_fetcher() is None
    assert len(rec.calls) == 1
    # Second call must not retry pip — failure is cached.
    _forbid_subprocess(monkeypatch)
    assert _resolve_scrapling_fetcher() is None


def test_missing_pip_degrades_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_state(monkeypatch)

    def _no_pip(argv, **kwargs):  # noqa: ANN001, ANN003
        raise FileNotFoundError("no pip")

    monkeypatch.setattr(subprocess, "run", _no_pip)
    assert _resolve_scrapling_fetcher() is None


def test_redact_strips_urls_and_tokens() -> None:
    raw = "fetch https://user:secret@pypi.internal/simple failed token ABCDEFGHIJKLMNOPQRSTUVWXYZ12"
    out = _redact(raw)
    assert "https://" not in out
    assert "secret" not in out
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in out
    assert "<url>" in out and "<token>" in out
