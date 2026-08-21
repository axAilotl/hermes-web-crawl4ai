"""Stealth-escalation dependency ownership: plugin-owned self-install.

The plugin must not depend on core's ``tools.lazy_deps`` allowlist for its
scrapling stealth leg (that allowlist is reserved for in-core backends).
These tests pin the paths of ``_import_scrapling_fetcher``: already
importable, missing-and-installed, pip failure, and the no-install kill
switch. No test may reach a real pip process.
"""

from __future__ import annotations

import sys
import types

import pytest

from hermes_web_crawl4ai.provider import _import_scrapling_fetcher


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


def _forbid_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_run(*args, **kwargs):  # noqa: ANN002, ANN003
        pytest.fail("test attempted a real subprocess.run")

    monkeypatch.setattr("subprocess.run", _fail_run)


def test_import_returns_fetcher_when_scrapling_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_scrapling_module(monkeypatch)
    _forbid_subprocess(monkeypatch)
    assert _import_scrapling_fetcher() is _FakeFetcher


def test_import_fails_soft_when_no_install_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "scrapling", raising=False)
    monkeypatch.setenv("HERMES_WEB_CRAWL4AI_NO_INSTALL", "1")
    _forbid_subprocess(monkeypatch)
    assert _import_scrapling_fetcher() is None


def test_import_self_installs_then_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "scrapling", raising=False)
    monkeypatch.delenv("HERMES_WEB_CRAWL4AI_NO_INSTALL", raising=False)

    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        calls.append(list(argv))
        # the pip run succeeds and makes scrapling importable from now on
        _install_fake_scrapling_module(monkeypatch)
        return _Proc(returncode=0)

    monkeypatch.setattr("subprocess.run", _fake_run)
    fetcher = _import_scrapling_fetcher()
    assert fetcher is _FakeFetcher
    assert len(calls) == 1
    # argv is [sys.executable, "-m", "pip", "install", "--quiet", spec]
    assert calls[0][-1] == "scrapling[fetchers]==0.4.14"
    assert calls[0][-4:-2] == ["pip", "install"]


def test_import_fails_soft_when_pip_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "scrapling", raising=False)
    monkeypatch.delenv("HERMES_WEB_CRAWL4AI_NO_INSTALL", raising=False)
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: _Proc(returncode=1, stderr="simulated pip failure")
    )
    assert _import_scrapling_fetcher() is None
