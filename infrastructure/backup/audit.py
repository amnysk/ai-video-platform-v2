"""audit 行（JSON 1 行）と失敗通知。通知は既定で無効（環境変数を置かない限り何も送らない）。"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from contracts.backup import ENV_NOTIFY_CMD, ENV_NOTIFY_WEBHOOK

NOTIFY_TIMEOUT_SECONDS = 15


class Notifier(Protocol):
    def notify(self, event: Mapping[str, Any]) -> None: ...


class NullNotifier:
    def notify(self, event: Mapping[str, Any]) -> None:
        return None


class CommandNotifier:
    """``AVP_BACKUP_NOTIFY_CMD`` を argv 分割して実行し、event の JSON を stdin に渡す。"""

    def __init__(self, command: str) -> None:
        self._argv = shlex.split(command)

    def notify(self, event: Mapping[str, Any]) -> None:
        subprocess.run(  # noqa: S603
            self._argv,
            input=json.dumps(event, ensure_ascii=False).encode(),
            timeout=NOTIFY_TIMEOUT_SECONDS,
            check=False,
            capture_output=True,
        )


class WebhookNotifier:
    def __init__(self, url: str) -> None:
        if urlparse(url).scheme not in {"http", "https"}:
            raise ValueError("webhook must be http(s)")
        self._url = url

    def notify(self, event: Mapping[str, Any]) -> None:
        req = urllib.request.Request(  # noqa: S310
            self._url,
            data=json.dumps(event, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=NOTIFY_TIMEOUT_SECONDS).close()  # noqa: S310


def notifier_from_env(env: Mapping[str, str] | None = None) -> Notifier:
    env = os.environ if env is None else env
    cmd, hook = env.get(ENV_NOTIFY_CMD), env.get(ENV_NOTIFY_WEBHOOK)
    parts: list[Notifier] = []
    if cmd:
        parts.append(CommandNotifier(cmd))
    if hook:
        parts.append(WebhookNotifier(hook))
    return FanOut(parts) if parts else NullNotifier()


class FanOut:
    def __init__(self, parts: list[Notifier]) -> None:
        self._parts = parts

    def notify(self, event: Mapping[str, Any]) -> None:
        errors = 0
        for p in self._parts:
            try:
                p.notify(event)
            except Exception:  # noqa: BLE001 - 1 つの通知先の失敗が他を止めない
                errors += 1
        if errors:
            raise RuntimeError(f"{errors} notifier(s) failed")


class AuditLog:
    """JSON 1 行を stderr と（あれば）ファイルへ。失敗してもバックアップ自体は止めない。"""

    def __init__(
        self,
        path: Path | None = None,
        *,
        write: Callable[[str], None] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._path = path
        self._write = write or (lambda line: print(line, file=sys.stderr, flush=True))
        self._now = now

    def emit(self, event: str, **fields: Any) -> None:
        line = json.dumps(
            {"ts": self._now().isoformat(), "component": "backup", "event": event, **fields},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        self._write(line)
        if self._path is not None:
            try:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
