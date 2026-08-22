"""Stealth-escalation dependency ownership: plugin-owned self-install.

The plugin must not depend on core's ``tools.lazy_deps`` allowlist for its
scrapling stealth leg (that allowlist is reserved for in-core backends).
These tests pin ``_resolve_scrapling_fetcher``'s contract: exact pip argv
and subprocess kwargs, allowlisted child environment, single-flight under
GENUINELY OVERLAPPING concurrent callers, failure caching, soft degradation
(broken installs, missing pip), kill-switch casing, the shared-future async
path, and inter-process lock semantics. No test may reach a real pip.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import threading
import types

import pytest

import hermes_web_crawl4ai.provider as provider
from hermes_web_crawl4ai.provider import _resolve_scrapling_fetcher


class _FakeFetcher:
    """Stands in for scrapling.StealthyFetcher in sys.modules."""

    marker = "fake-stealthyfetcher"


class _Proc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = "https://user:secret@pypi.internal/simple went away"


def _install_fake_scrapling_module(monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = types.ModuleType("scrapling")
    pkg.StealthyFetcher = _FakeFetcher
    monkeypatch.setitem(sys.modules, "scrapling", pkg)


def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider, "_scrapling_state", {"resolved": False, "fetcher": None})
    monkeypatch.delitem(sys.modules, "scrapling", raising=False)
    monkeypatch.delenv(provider._SCRAPLING_INSTALL_ENV, raising=False)
    monkeypatch.setattr(provider, "_scrapling_resolve_task", None)


def _forbid_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_run(*args, **kwargs):  # noqa: ANN002, ANN003
        pytest.fail("test attempted a real subprocess.run")

    monkeypatch.setattr(subprocess, "run", _fail_run)


class _BlockingPipRecorder:
    """Fake pip that BLOCKS inside the call until N callers are inside the
    resolver, so concurrency is genuinely forced (a decorative lock would
    let multiple pip calls through)."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        returncode: int = 0,
        fix_imports: bool = True,
        callers_to_admit: int = 1,
    ) -> None:
        self.calls = 0
        self.last_call: tuple[list[str], dict] | None = None
        self._monkeypatch = monkeypatch
        self._fix_imports = fix_imports
        self._gate = threading.Barrier(callers_to_admit, timeout=5)
        self._returncode = returncode
        monkeypatch.setattr(subprocess, "run", self)

    def __call__(self, argv, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.last_call = (list(argv), kwargs)
        # Block INSIDE pip until the expected number of callers are racing.
        with contextlib.suppress(threading.BrokenBarrierError):
            self._gate.wait()
        if self._returncode == 0 and self._fix_imports:
            _install_fake_scrapling_module(self._monkeypatch)
        return _Proc(returncode=self._returncode)


# ── argv / environment contract ───────────────────────────────────────


def test_pip_argv_and_kwargs_are_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_state(monkeypatch)
    rec = _BlockingPipRecorder(monkeypatch)
    fetcher = _resolve_scrapling_fetcher()
    assert fetcher is _FakeFetcher
    assert rec.calls == 1
    argv, kwargs = rec.last_call
    assert argv == [
        sys.executable,
        "-I",
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
    # Allowlisted child environment — no proxies, no TLS overrides, no
    # PIP_*/PYTHON* ambient state beyond the two deliberate knobs. The
    # platform-aware allowlist applies (POSIX subset on this host).
    child_env = kwargs["env"]
    expected_allow = (
        provider._PIP_ENV_ALLOWLIST_WINDOWS
        if sys.platform == "win32"
        else provider._PIP_ENV_ALLOWLIST_POSIX
    )
    assert set(child_env) - {"PIP_NO_INPUT", "PIP_DISABLE_PIP_VERSION_CHECK"} <= set(
        expected_allow
    )
    assert child_env["PIP_NO_INPUT"] == "1"
    assert not any(k.upper().startswith("HTTPS_PROXY") for k in child_env)


def test_pip_env_is_a_pure_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://evil:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/evil.pem")
    monkeypatch.setenv("PIP_INDEX_URL", "https://evil.example.com/simple")
    monkeypatch.setenv("PYTHONPATH", "/tmp/somewhere")
    env = provider._pip_env()
    assert "HTTPS_PROXY" not in env
    assert "SSL_CERT_FILE" not in env
    assert "PIP_INDEX_URL" not in env
    assert "PYTHONPATH" not in env
    assert env["PIP_NO_INPUT"] == "1"


# ── single-flight contract ────────────────────────────────────────────


def test_concurrent_calls_with_genuine_overlap_install_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 4 threads; pip BLOCKS until 4 callers are inside the resolver. The
    # inter-process lock is disabled for this test so ONLY the in-process
    # threading.Lock provides single-flight (removing THAT lock then yields
    # 4 pip calls and fails the test — verified by reviewer probes and by
    # construction).
    _reset_state(monkeypatch)
    monkeypatch.setattr(provider, "_interprocess_lock", lambda: None)
    rec = _BlockingPipRecorder(monkeypatch, callers_to_admit=4)
    results: list[object] = []
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def _worker() -> None:
        try:
            start.wait()
            results.append(_resolve_scrapling_fetcher())
        except BaseException as exc:  # noqa: BLE001 — record, don't swallow
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "resolver deadlocked"

    assert not errors, f"worker exceptions: {errors!r}"
    assert len(results) == 4
    assert rec.calls == 1, f"single-flight violated: {rec.calls} pip calls"
    assert all(r is _FakeFetcher for r in results)


def test_failed_install_is_cached_for_process_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_state(monkeypatch)
    _BlockingPipRecorder(monkeypatch, returncode=1)
    assert _resolve_scrapling_fetcher() is None
    _forbid_subprocess(monkeypatch)  # second call must not retry pip
    assert _resolve_scrapling_fetcher() is None


def test_async_waiters_share_one_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    # 4 ASYNC callers: exactly one executor job is created; all await the
    # same future. Executor submissions are counted explicitly — multiple
    # jobs serialized by locks would still pass a pip-call count alone.
    _reset_state(monkeypatch)
    monkeypatch.setattr(provider, "_interprocess_lock", lambda: None)
    rec = _BlockingPipRecorder(monkeypatch, callers_to_admit=4)
    submissions: list[object] = []

    async def _scenario() -> list[object]:
        start = asyncio.Barrier(4)
        loop = asyncio.get_running_loop()
        real_run_in_executor = loop.run_in_executor

        def _counting_run_in_executor(executor, func, *args):  # noqa: ANN001, ANN003
            submissions.append(func)
            return real_run_in_executor(executor, func, *args)

        loop.run_in_executor = _counting_run_in_executor  # type: ignore[method-assign]

        async def _worker() -> object:
            await start.wait()
            return await provider._resolve_scrapling_fetcher_async()

        return list(await asyncio.gather(*[_worker() for _ in range(4)]))

    results = asyncio.run(_scenario())
    assert rec.calls == 1
    assert len(submissions) == 1, f"expected 1 executor job, got {len(submissions)}"
    assert all(r is _FakeFetcher for r in results)
    assert provider._scrapling_resolve_task is not None


# ── degradation contract ──────────────────────────────────────────────


def test_fetcher_returned_when_scrapling_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_state(monkeypatch)
    _install_fake_scrapling_module(monkeypatch)
    _forbid_subprocess(monkeypatch)
    assert _resolve_scrapling_fetcher() is _FakeFetcher


def test_broken_install_degrades_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_state(monkeypatch)
    pkg = types.ModuleType("scrapling")

    def _getattr(name):
        raise RuntimeError("partially broken install")

    pkg.__getattr__ = _getattr  # type: ignore[method-assign]
    monkeypatch.setitem(sys.modules, "scrapling", pkg)
    # pip "succeeds" but the package stays broken (fix_imports=False)
    rec = _BlockingPipRecorder(monkeypatch, returncode=0, fix_imports=False)
    assert _resolve_scrapling_fetcher() is None
    assert rec.calls == 1
    _forbid_subprocess(monkeypatch)  # failure cached
    assert _resolve_scrapling_fetcher() is None


def test_missing_pip_degrades_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_state(monkeypatch)

    def _no_pip(argv, **kwargs):  # noqa: ANN001, ANN003
        raise FileNotFoundError("no pip")

    monkeypatch.setattr(subprocess, "run", _no_pip)
    assert _resolve_scrapling_fetcher() is None


def test_no_install_switch_skips_pip_in_any_casing(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("1", "TRUE", "Yes"):
        _reset_state(monkeypatch)
        monkeypatch.setenv(provider._SCRAPLING_INSTALL_ENV, value)
        _forbid_subprocess(monkeypatch)
        assert _resolve_scrapling_fetcher() is None, value


def test_no_install_switch_still_uses_present_scrapling(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_state(monkeypatch)
    _install_fake_scrapling_module(monkeypatch)
    monkeypatch.setenv(provider._SCRAPLING_INSTALL_ENV, "1")
    _forbid_subprocess(monkeypatch)
    assert _resolve_scrapling_fetcher() is _FakeFetcher


# ── inter-process lock ────────────────────────────────────────────────


def test_interprocess_lock_rechecks_after_acquiring(monkeypatch: pytest.MonkeyPatch) -> None:
    # Process B acquires the flock after process A installed; the post-lock
    # recheck must skip pip entirely.
    _reset_state(monkeypatch)
    calls: list[list[str]] = []

    class _FakeLock:
        def release(self) -> None:
            pass

    def _fake_lock():
        # Simulate: while we waited, ANOTHER process finished the install.
        _install_fake_scrapling_module(monkeypatch)
        return _FakeLock()

    monkeypatch.setattr(provider, "_interprocess_lock", _fake_lock)

    def _fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        calls.append(list(argv))
        return _Proc(returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _resolve_scrapling_fetcher() is _FakeFetcher
    assert calls == [], "post-lock recheck failed — redundant pip after cross-process install"
