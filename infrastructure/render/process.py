"""render の重い子プロセスを監督付きで走らせる（Phase 5）。

``infrastructure.providers.process.SubprocessRunner`` との違い:

- stdout / stderr を**作業ディレクトリのログファイル**へ流す（長時間の encode の出力を
  メモリに溜めない）。結果には末尾だけを持つ
- 走っている間、一定間隔（既定 5 秒、上限 10 秒）で heartbeat callback を呼ぶ
  （Temporal activity の heartbeat 用）
- 終わり方を ``ProcessOutcome`` で区別して返す（正常 / 非0終了 / signal / 時間切れ / 容量不足）

cancel（``asyncio.CancelledError``）はプロセスグループへ SIGTERM → 猶予 → SIGKILL を送り、
回収し終えてから**そのまま再送出**する。cancel は失敗ではないので outcome にしない。
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import os
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: SIGTERM のあと SIGKILL に上げるまでの既定の猶予（秒）。
DEFAULT_GRACE_SECONDS = 5.0
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5.0
#: heartbeat 間隔の上限。activity の heartbeat_timeout（60 秒）より十分短く保つ。
MAX_HEARTBEAT_INTERVAL_SECONDS = 10.0
#: 結果に持つログ末尾の上限（バイト）。
DEFAULT_TAIL_BYTES = 16 * 1024

#: 書き込み先の容量不足を示す stderr の文言（ENOSPC の strerror）。
NO_SPACE_MARKER = "No space left on device"

HeartbeatCallback = Callable[[], object]


class ProcessOutcome(enum.StrEnum):
    SUCCEEDED = "succeeded"
    #: 0 以外の終了コード
    FAILED = "failed"
    #: signal で落ちた（cancel 以外）
    SIGNALED = "signaled"
    TIMED_OUT = "timed_out"
    #: 容量不足（ENOSPC）。終了コードより優先して判定する
    NO_SPACE = "no_space"


@dataclass(frozen=True, slots=True)
class SupervisedResult:
    outcome: ProcessOutcome
    #: 終了コード。signal で終わったときは ``None``
    exit_code: int | None
    #: 終了させた signal 番号。時間切れで止めた場合もここに入る
    signal: int | None
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path
    #: stderr の末尾（``DEFAULT_TAIL_BYTES`` まで、UTF-8 として置換デコード）
    stderr_tail: str


class SupervisedProcessRunner:
    def __init__(
        self,
        *,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        tail_bytes: int = DEFAULT_TAIL_BYTES,
    ) -> None:
        if not 0 < heartbeat_interval_seconds <= MAX_HEARTBEAT_INTERVAL_SECONDS:
            raise ValueError(
                f"heartbeat interval must be in (0, {MAX_HEARTBEAT_INTERVAL_SECONDS}] seconds"
            )
        if grace_seconds < 0 or tail_bytes <= 0:
            raise ValueError("grace_seconds must be >= 0 and tail_bytes > 0")
        self.grace_seconds = grace_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.tail_bytes = tail_bytes

    async def run(
        self,
        argv: Sequence[str],
        *,
        log_dir: Path,
        log_name: str,
        timeout_seconds: float,
        heartbeat: HeartbeatCallback | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> SupervisedResult:
        """``argv`` を shell 無しで起動し、終わるまで監督する。

        ログは ``log_dir/<log_name>.stdout.log`` / ``.stderr.log``（既存なら上書き）。
        """
        if isinstance(argv, str | bytes) or not argv:
            raise ValueError("argv must be a non-empty sequence of arguments")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        stdout_path = log_dir / f"{log_name}.stdout.log"
        stderr_path = log_dir / f"{log_name}.stderr.log"
        started = time.monotonic()
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=dict(env) if env is not None else None,
                cwd=cwd,
                start_new_session=True,
            )
        timed_out = False
        try:
            deadline = started + timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                wait = min(self.heartbeat_interval_seconds, remaining)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(process.wait()), timeout=wait)
                    break
                if heartbeat is not None:
                    heartbeat()
            if timed_out:
                await self._terminate_group(process)
            else:
                # 本体が終わっても、グループに残った子孫がいれば止める（孤児を残さない）
                self._signal_group(process.pid, signal.SIGKILL)
        except BaseException:
            # cancel（CancelledError）・heartbeat の例外を含む。子孫を残さずに伝える。
            await asyncio.shield(self._terminate_group(process))
            raise
        returncode = process.returncode
        duration = time.monotonic() - started
        tail = _read_tail(stderr_path, self.tail_bytes)
        exit_code = returncode if returncode is not None and returncode >= 0 else None
        sig = -returncode if returncode is not None and returncode < 0 else None
        if timed_out:
            outcome = ProcessOutcome.TIMED_OUT
        elif NO_SPACE_MARKER in tail:
            outcome = ProcessOutcome.NO_SPACE
        elif sig is not None:
            outcome = ProcessOutcome.SIGNALED
        elif exit_code == 0:
            outcome = ProcessOutcome.SUCCEEDED
        else:
            outcome = ProcessOutcome.FAILED
        return SupervisedResult(
            outcome=outcome,
            exit_code=exit_code,
            signal=sig,
            duration_seconds=duration,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stderr_tail=tail,
        )

    async def _terminate_group(self, process: asyncio.subprocess.Process) -> None:
        """SIGTERM → 猶予 → SIGKILL をグループへ送り、本体を回収する。

        本体が SIGTERM で先に終わっても、SIGTERM を無視する子孫がいるかもしれないので
        SIGKILL は常にグループへ送る（居なければ何もしない）。
        """
        self._signal_group(process.pid, signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=self.grace_seconds)
        self._signal_group(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await process.wait()

    @staticmethod
    def _signal_group(pgid: int, sig: int) -> None:
        # start_new_session=True なのでグループ ID は本体の pid。本体を回収した後でも
        # グループに子孫が残っていれば届く。
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig)


def _read_tail(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - limit))
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "MAX_HEARTBEAT_INTERVAL_SECONDS",
    "NO_SPACE_MARKER",
    "HeartbeatCallback",
    "ProcessOutcome",
    "SupervisedProcessRunner",
    "SupervisedResult",
]
