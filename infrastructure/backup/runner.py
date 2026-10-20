"""外部コマンドの実行を 1 つの口に寄せる（テストは fake を差す。実機の本番には届かせない）。"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_TIMEOUT_SECONDS = 1800.0
CHUNK = 1024 * 1024


@dataclass(frozen=True)
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class FileResult:
    returncode: int
    sha256: str
    size: int
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin_path: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> CmdResult: ...

    def run_to_file(
        self, argv: Sequence[str], dest: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> FileResult: ...


class SubprocessRunner:
    """shell を介さない。timeout / 起動失敗は returncode=127 の結果にして呼び出し側で失敗にする。"""

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin_path: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> CmdResult:
        try:
            if stdin_path is None:
                done = subprocess.run(  # noqa: S603
                    list(argv), capture_output=True, text=True, timeout=timeout, check=False
                )
            else:
                with stdin_path.open("rb") as handle:
                    done = subprocess.run(  # noqa: S603
                        list(argv),
                        stdin=handle,
                        capture_output=True,
                        timeout=timeout,
                        check=False,
                    )
                return CmdResult(
                    done.returncode,
                    done.stdout.decode("utf-8", "replace"),
                    done.stderr.decode("utf-8", "replace"),
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CmdResult(127, "", f"{argv[0]}: {type(exc).__name__}")
        return CmdResult(done.returncode, done.stdout, done.stderr)

    def run_to_file(
        self, argv: Sequence[str], dest: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> FileResult:
        digest = hashlib.sha256()
        size = 0
        try:
            with dest.open("xb") as out:
                proc = subprocess.Popen(  # noqa: S603
                    list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                assert proc.stdout is not None and proc.stderr is not None
                for chunk in iter(lambda: proc.stdout.read(CHUNK), b""):  # type: ignore[union-attr]
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
                err = proc.stderr.read().decode("utf-8", "replace")
                rc = proc.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return FileResult(127, digest.hexdigest(), size, f"{argv[0]}: {type(exc).__name__}")
        return FileResult(rc, digest.hexdigest(), size, err)
