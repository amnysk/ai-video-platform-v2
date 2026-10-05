"""起動点の接続（worker_entry・API serve・Temporal Core の転送 / ADR-0040 §1）。

理由は docs/testing/logging-rationale.md。
"""

from __future__ import annotations

import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest

from infrastructure.logging import setup
from infrastructure.runtime import worker_entry
from tests.support.log_capture import capture_json


def _module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> str:
    monkeypatch.syspath_prepend(str(tmp_path))
    name = f"fake_worker_{uuid.uuid4().hex}"
    (tmp_path / f"{name}.py").write_text(
        "async def main():\n" + textwrap.indent(textwrap.dedent(body), "    "), encoding="utf-8"
    )
    return name


def test_worker_entry_emits_service_started_and_stopped(tmp_path, monkeypatch) -> None:
    name = _module(tmp_path, monkeypatch, "return None\n")
    with capture_json() as logs:
        assert worker_entry.run(name, env={"AVP_GIT_REVISION": "abc"}) == 0
    started, stopped = logs.events("service.started"), logs.events("service.stopped")
    assert started[0]["message"] == f"worker {name} starting revision=abc"  # 既存の文言
    assert stopped[0]["outcome"] == "succeeded"


def test_worker_entry_emits_start_failed_for_an_early_crash(tmp_path, monkeypatch) -> None:
    name = _module(tmp_path, monkeypatch, "raise SystemExit('missing FAL_KEY')\n")
    with capture_json() as logs:
        code = worker_entry.run(name, sleep=lambda _: None, env={})
    assert code == 1
    [failed] = logs.events("service.start_failed")
    assert failed["level"] == "ERROR" and failed["error_type"] == "SystemExit"
    assert "missing FAL_KEY" in failed["message"]


def test_cli_configures_logging_once_before_running(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(worker_entry, "configure_logging", lambda: calls.append("configure"))
    monkeypatch.setattr(worker_entry, "run", lambda name: calls.append(name) or 0)
    monkeypatch.setattr(worker_entry.signal, "signal", lambda *a: None)
    assert worker_entry.cli(["workers.x.run_worker"]) == 0
    assert calls == ["configure", "workers.x.run_worker"]


def test_api_serve_uses_the_common_setup_and_no_uvicorn_logging(monkeypatch) -> None:
    from apps.api import serve

    seen: dict[str, Any] = {}
    monkeypatch.setattr(serve, "configure_logging", lambda: seen.setdefault("configured", True))
    monkeypatch.setattr(serve, "configure_uvicorn_loggers", lambda: seen.setdefault("uv", True))
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    serve.main()
    assert seen["configured"] and seen["uv"]
    assert seen["app"] == "apps.api.main:app"
    assert seen["log_config"] is None and seen["access_log"] is False


def test_core_forwarding_is_only_installed_after_configure(monkeypatch) -> None:
    installed: list[bool] = []
    monkeypatch.setattr(setup, "install_core_log_forwarding", lambda: installed.append(True))
    monkeypatch.setitem(setup._state, "configured", False)
    setup.ensure_core_log_forwarding()
    assert installed == []
    monkeypatch.setitem(setup._state, "configured", True)
    monkeypatch.setitem(setup._state, "forward", True)
    setup.ensure_core_log_forwarding()
    assert installed == [True]


async def test_connect_installs_forwarding_before_the_first_connect(monkeypatch) -> None:
    from infrastructure.config import Settings
    from infrastructure.temporal import connect

    order: list[str] = []
    monkeypatch.setattr(connect, "ensure_core_log_forwarding", lambda: order.append("forward"))

    async def fake_connect(*_: Any, **__: Any) -> str:
        order.append("connect")
        return "client"

    assert await connect.connect_with_retry(Settings(), connect=fake_connect) == "client"
    assert order == ["forward", "connect"]


def test_worker_interceptors_is_the_single_entry() -> None:
    from infrastructure.logging.temporal import ActivityLoggingInterceptor, worker_interceptors

    [only] = worker_interceptors()
    assert isinstance(only, ActivityLoggingInterceptor)
