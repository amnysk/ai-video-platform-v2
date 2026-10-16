"""外部プロセスの起動。provider アダプタから I/O を切り離すための層。

``ProcessRunner`` を挟むことで、アダプタの単体テストは本物のプロセスを
起動せずに argv・stdin・env を検証できる（INV-18）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

#: SIGTERM のあと SIGKILL に上げるまでの既定の猶予（秒）。
DEFAULT_GRACE_SECONDS = 5.0


class ProcessTimeout(Exception):
    """制限時間内に終わらなかった。domain の例外へはアダプタが変換する。"""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        env: Mapping[str, str],
        timeout_seconds: int,
        cwd: str | None = None,
    ) -> ProcessResult: ...


class SubprocessRunner:
    """``asyncio.create_subprocess_exec`` で起動する実装。

    - shell を使わない。argv は配列固定（shell injection の回避）
    - ``start_new_session=True`` で独自プロセスグループを作り、タイムアウト時は
      ``os.killpg`` で**子孫ごと**止める。codex は MCP サーバ等の子を作るため、
      プロセス単体の kill では殺し残す
    - SIGTERM → 猶予 → SIGKILL のエスカレーション
    - タイムアウトに限らず、**cancel を含むあらゆる例外**で同じ停止を行ってから伝える
    """

    def __init__(self, *, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> None:
        self.grace_seconds = grace_seconds

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        env: Mapping[str, str],
        timeout_seconds: int,
        cwd: str | None = None,
    ) -> ProcessResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin.encode("utf-8")), timeout=timeout_seconds
            )
        except TimeoutError as exc:
            await self._terminate_group(process)
            raise ProcessTimeout(f"process exceeded {timeout_seconds}s") from exc
        except BaseException:
            # cancel（CancelledError）・KeyboardInterrupt を含む。子孫を残さずに伝える。
            # 再度 cancel されても停止処理は shield の内側で完走させる。
            await asyncio.shield(self._terminate_group(process))
            raise
        return ProcessResult(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    async def _terminate_group(self, process: asyncio.subprocess.Process) -> None:
        """プロセスグループごと止める。孫を残さないのがここの目的。"""
        for sig, wait in ((signal.SIGTERM, self.grace_seconds), (signal.SIGKILL, 2.0)):
            if not self._signal_group(process, sig):
                return
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=wait)
                return
        with contextlib.suppress(Exception):
            await process.wait()

    @staticmethod
    def _signal_group(process: asyncio.subprocess.Process, sig: int) -> bool:
        """まだ生きていれば送る。送れたら ``True``。"""
        if process.returncode is not None:
            return False
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError):
            return False
        return True
