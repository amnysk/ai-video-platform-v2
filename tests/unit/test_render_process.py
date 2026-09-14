"""SupervisedProcessRunner: 本物の小さな子プロセスで監督の挙動を確かめる。"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from infrastructure.render.process import ProcessOutcome, SupervisedProcessRunner


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # 回収前の zombie は死んでいる扱い
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


async def _wait_pidfile(pidfile: Path) -> int:
    for _ in range(500):
        if pidfile.exists() and pidfile.read_text().strip():
            return int(pidfile.read_text().strip())
        await asyncio.sleep(0.01)
    raise AssertionError("child did not write its pid")


async def _gone(pid: int, seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.02)
    return False


def _spawner(pidfile: Path, *, ignore_term: bool = False) -> list[str]:
    """孫（sleep 30）を作って pid を書き、自分も待つ python の子。"""
    code = (
        "import signal, subprocess, sys, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + "p = subprocess.Popen(['sleep', '30'])\n"
        + f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        + "time.sleep(30)\n"
    )
    return [sys.executable, "-c", code]


async def test_success_writes_logs_and_captures_the_tail(tmp_path: Path) -> None:
    runner = SupervisedProcessRunner(tail_bytes=8)
    result = await runner.run(
        [sys.executable, "-c", "import sys; print('out'); sys.stderr.write('0123456789abc')"],
        log_dir=tmp_path,
        log_name="job",
        timeout_seconds=30,
    )
    assert result.outcome is ProcessOutcome.SUCCEEDED
    assert result.exit_code == 0 and result.signal is None
    assert result.stdout_path.read_text() == "out\n"
    assert result.stderr_path.read_text() == "0123456789abc"
    assert result.stderr_tail == "56789abc"


async def test_nonzero_exit_is_failed(tmp_path: Path) -> None:
    result = await SupervisedProcessRunner().run(
        [sys.executable, "-c", "raise SystemExit(3)"],
        log_dir=tmp_path,
        log_name="job",
        timeout_seconds=30,
    )
    assert result.outcome is ProcessOutcome.FAILED
    assert result.exit_code == 3


async def test_no_space_in_stderr_is_distinct(tmp_path: Path) -> None:
    code = "import sys; sys.stderr.write('out.mp4: No space left on device\\n'); sys.exit(1)"
    result = await SupervisedProcessRunner().run(
        [sys.executable, "-c", code], log_dir=tmp_path, log_name="job", timeout_seconds=30
    )
    assert result.outcome is ProcessOutcome.NO_SPACE


async def test_killed_by_signal_is_signaled(tmp_path: Path) -> None:
    code = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    result = await SupervisedProcessRunner().run(
        [sys.executable, "-c", code], log_dir=tmp_path, log_name="job", timeout_seconds=30
    )
    assert result.outcome is ProcessOutcome.SIGNALED
    assert result.exit_code is None and result.signal == 9


async def test_heartbeat_is_called_at_the_configured_cadence(tmp_path: Path) -> None:
    beats: list[float] = []
    runner = SupervisedProcessRunner(heartbeat_interval_seconds=0.1)
    await runner.run(
        ["sleep", "0.65"],
        log_dir=tmp_path,
        log_name="job",
        timeout_seconds=30,
        heartbeat=lambda: beats.append(time.monotonic()),
    )
    assert 4 <= len(beats) <= 7
    gaps = [b - a for a, b in zip(beats, beats[1:], strict=False)]
    assert all(gap < 0.3 for gap in gaps)


def test_heartbeat_interval_is_capped() -> None:
    with pytest.raises(ValueError):
        SupervisedProcessRunner(heartbeat_interval_seconds=11)


async def test_argv_must_be_a_list_not_a_shell_string(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await SupervisedProcessRunner().run(
            "sleep 1", log_dir=tmp_path, log_name="job", timeout_seconds=5
        )


async def test_timeout_kills_the_whole_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    runner = SupervisedProcessRunner(grace_seconds=0.3, heartbeat_interval_seconds=0.1)
    task = asyncio.create_task(
        runner.run(_spawner(pidfile), log_dir=tmp_path, log_name="job", timeout_seconds=1.0)
    )
    grandchild = await _wait_pidfile(pidfile)
    result = await task
    assert result.outcome is ProcessOutcome.TIMED_OUT
    assert await _gone(grandchild)


async def test_cancellation_kills_the_group_even_if_sigterm_is_ignored(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    runner = SupervisedProcessRunner(grace_seconds=0.3, heartbeat_interval_seconds=0.1)
    task = asyncio.create_task(
        runner.run(
            _spawner(pidfile, ignore_term=True),
            log_dir=tmp_path,
            log_name="job",
            timeout_seconds=60,
        )
    )
    grandchild = await _wait_pidfile(pidfile)
    assert _alive(grandchild)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _gone(grandchild)


async def test_leftover_descendants_are_killed_after_normal_exit(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    result = await SupervisedProcessRunner().run(
        ["sh", "-c", f"sleep 30 & echo $! > {pidfile}"],
        log_dir=tmp_path,
        log_name="job",
        timeout_seconds=30,
    )
    assert result.outcome is ProcessOutcome.SUCCEEDED
    assert await _gone(int(pidfile.read_text().strip()))


async def test_heartbeat_failure_stops_the_process(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"

    def boom() -> None:
        raise RuntimeError("heartbeat failed")

    runner = SupervisedProcessRunner(grace_seconds=0.3, heartbeat_interval_seconds=0.1)
    task = asyncio.create_task(
        runner.run(
            _spawner(pidfile), log_dir=tmp_path, log_name="job", timeout_seconds=60, heartbeat=boom
        )
    )
    with pytest.raises(RuntimeError):
        await task
    assert await _gone(await _wait_pidfile(pidfile))


def test_signal_group_skips_kill_when_the_group_is_empty(monkeypatch) -> None:
    import os as _os
    import signal as _signal

    from infrastructure.render.process import SupervisedProcessRunner

    sent: list[int] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        sent.append(sig)
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(_os, "killpg", fake_killpg)
    SupervisedProcessRunner._signal_group(12345, _signal.SIGKILL)
    assert sent == [0]


def test_signal_group_kills_when_members_remain(monkeypatch) -> None:
    import os as _os
    import signal as _signal

    from infrastructure.render.process import SupervisedProcessRunner

    sent: list[int] = []
    monkeypatch.setattr(_os, "killpg", lambda pgid, sig: sent.append(sig))
    SupervisedProcessRunner._signal_group(12345, _signal.SIGKILL)
    assert sent == [0, _signal.SIGKILL]
