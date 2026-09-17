"""infrastructure.runtime.worker_entry.run の単体検査。

API: ``run(module_name, *, clock=time.monotonic, sleep=time.sleep, env=os.environ) -> int``
clock は開始時と終了（失敗）時に呼ばれる想定。
"""

from __future__ import annotations

import itertools
import logging
import textwrap
import uuid
from pathlib import Path

import pytest

from infrastructure.runtime.worker_entry import run


@pytest.fixture
def make_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(tmp_path))

    def _make(body: str) -> str:
        name = f"fake_worker_{uuid.uuid4().hex}"
        (tmp_path / f"{name}.py").write_text(
            "async def main():\n" + textwrap.indent(textwrap.dedent(body), "    "), encoding="utf-8"
        )
        return name

    return _make


def _clock(*values: float):
    it = itertools.chain(values, itertools.repeat(values[-1]))
    return lambda: next(it)


class Sleeps(list):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def test_normal_completion_returns_0(make_module) -> None:
    sleeps = Sleeps()
    assert run(make_module("return None\n"), clock=_clock(0, 1), sleep=sleeps, env={}) == 0
    assert sleeps == []


def test_early_system_exit_backs_off(make_module, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)
    sleeps = Sleeps()
    code = run(
        make_module('raise SystemExit("missing FAL_KEY")\n'),
        clock=_clock(0, 1),
        sleep=sleeps,
        env={},
    )
    assert code != 0
    assert sleeps == [60]
    assert "missing FAL_KEY" in caplog.text


def test_exception_after_min_uptime_exits_without_sleep(make_module, caplog) -> None:
    caplog.set_level(logging.ERROR)
    sleeps = Sleeps()
    code = run(
        make_module('raise RuntimeError("crash")\n'), clock=_clock(0, 100), sleep=sleeps, env={}
    )
    assert code != 0
    assert sleeps == []
    assert "RuntimeError" in caplog.text


def test_early_exception_backs_off(make_module) -> None:
    sleeps = Sleeps()
    code = run(
        make_module('raise RuntimeError("crash")\n'), clock=_clock(0, 2), sleep=sleeps, env={}
    )
    assert code != 0
    assert sleeps == [60]


def test_keyboard_interrupt_returns_0_without_sleep(make_module) -> None:
    sleeps = Sleeps()
    assert (
        run(make_module("raise KeyboardInterrupt\n"), clock=_clock(0, 1), sleep=sleeps, env={}) == 0
    )
    assert sleeps == []


def test_env_overrides(make_module) -> None:
    env = {"AVP_WORKER_MIN_UPTIME_SECONDS": "200", "AVP_WORKER_FAILURE_BACKOFF_SECONDS": "5"}
    sleeps = Sleeps()
    code = run(
        make_module('raise SystemExit("missing")\n'), clock=_clock(0, 100), sleep=sleeps, env=env
    )
    assert code != 0
    assert sleeps == [5]


def test_sigterm_during_backoff_exits_0_promptly(make_module) -> None:
    """設定不足の待機中に ``docker compose stop``（SIGTERM）されても即 0 終了する。"""

    def interrupted_sleep(seconds: float) -> None:
        raise KeyboardInterrupt

    try:
        code = run(
            make_module('raise SystemExit("missing FAL_KEY")\n'),
            clock=_clock(0, 1),
            sleep=interrupted_sleep,
            env={},
        )
    except KeyboardInterrupt:
        pytest.fail("backoff 中の KeyboardInterrupt が run() の外へ漏れた")
    assert code == 0


def test_sigterm_handler_forwards_sigint(monkeypatch: pytest.MonkeyPatch) -> None:
    """SIGTERM で KeyboardInterrupt を直接投げると、イベントループの任意の地点で割り込み
    Temporal Worker の shutdown が戻らなくなる（実測: stop_grace_period まで待って SIGKILL）。
    SIGINT に転送し、asyncio.run の SIGINT 処理（main task の cancel）に任せる。"""
    import signal

    from infrastructure.runtime import worker_entry

    raised: list[int] = []
    monkeypatch.setattr(signal, "raise_signal", raised.append)
    worker_entry._on_sigterm(signal.SIGTERM, None)
    assert raised == [signal.SIGINT]
