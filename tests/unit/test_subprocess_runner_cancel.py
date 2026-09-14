"""SubprocessRunner は cancel でもプロセスグループごと止める（孫を残さない）。"""

from __future__ import annotations

import asyncio
import os

import pytest

from infrastructure.providers.process import SubprocessRunner


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_cancellation_terminates_the_process_group(tmp_path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    runner = SubprocessRunner(grace_seconds=0.5)
    task = asyncio.create_task(
        runner.run(
            ["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"],
            stdin="",
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            timeout_seconds=60,
        )
    )
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.01)
    grandchild = int(pidfile.read_text().strip())
    assert _alive(grandchild)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(300):
        if not _alive(grandchild):
            break
        await asyncio.sleep(0.01)
    assert not _alive(grandchild), "grandchild survived cancellation"
