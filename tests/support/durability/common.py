"""子プロセス worker と親テストが共有する設定・起動・停止の部品。

設定は環境変数で子プロセスへ渡す（``DURABILITY_*``）。DB は一時スキーマに閉じる
（``search_path``）。本番の task queue 名は使わない（呼び出し側が一意な名前を渡す）。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]

ENV_DB_URL = "DURABILITY_DB_URL"
ENV_SCHEMA = "DURABILITY_SCHEMA"
ENV_QUEUE = "DURABILITY_QUEUE"
ENV_STAGE_QUEUE = "DURABILITY_STAGE_QUEUE"
ENV_READY_FILE = "DURABILITY_READY_FILE"
ENV_STATE_FILE = "DURABILITY_STATE_FILE"
ENV_WORK_DIR = "DURABILITY_WORK_DIR"
#: fake 工程のうち signal を待って止まるもの（カンマ区切りの workflow 名）
ENV_BLOCK_STAGES = "DURABILITY_BLOCK_STAGES"
#: fake uploader の 1 チャンクあたりの遅延（秒）
ENV_CHUNK_DELAY = "DURABILITY_CHUNK_DELAY"


def schema_session_factory(url: str, schema: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        url, connect_args={"options": f"-c search_path={schema}"}, hide_parameters=True
    )
    return async_sessionmaker(engine, expire_on_commit=False)


def mark_ready() -> None:
    path = os.environ.get(ENV_READY_FILE)
    if path:
        Path(path).write_text(str(os.getpid()))


@dataclass
class WorkerProcess:
    """``python -m <module>`` の worker。ready ファイルが書かれるまで待つ。SIGKILL で殺す。"""

    module: str
    env: dict[str, str]
    log_path: Path
    proc: subprocess.Popen[bytes] | None = None

    def start(self, *, ready_timeout: float = 60.0) -> WorkerProcess:
        ready = self.log_path.with_suffix(".ready")
        ready.unlink(missing_ok=True)
        env = {**os.environ, **self.env, ENV_READY_FILE: str(ready)}
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(REPO_ROOT), os.environ.get("PYTHONPATH", "")) if p
        )
        log = self.log_path.open("ab")
        self.proc = subprocess.Popen(  # noqa: S603 - 固定のテスト用モジュール
            [sys.executable, "-m", self.module],
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.close()
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if ready.exists():
                return self
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.module} exited early:\n{self.log_tail()}")
            time.sleep(0.1)
        self.kill()
        raise RuntimeError(f"{self.module} did not become ready:\n{self.log_tail()}")

    @property
    def pid(self) -> int:
        assert self.proc is not None
        return self.proc.pid

    def kill(self) -> None:
        """SIGKILL（後始末の機会を与えない）。"""
        if self.proc is not None and self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=10)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def log_tail(self, lines: int = 60) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""
