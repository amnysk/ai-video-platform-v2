# ruff: noqa: E501  (全角文字を 2 幅で数えるため、日本語の説明文が 100 を超える)
"""ホスト監視が共有する「OpenSearch に依存しない」警報口と heartbeat（ADR-0059 / INV-80）。

置き場所は一つ（AGENTS §8）。opensearch-guard / host-process-monitor / monitor-watch が import する。
標準ライブラリだけ。docker も network も使わない。

警報の出口（どれも OpenSearch・Temporal・DB を経由しない）:
  1. stderr に ``<N>avp-alert ...``（systemd の SyslogLevelPrefix で journal の priority になる）
  2. ``alerts.jsonl``（追記。発報の履歴）
  3. ``alerts-current.json``（いま有効な警報。``monitor-watch.py --show-alerts`` が読む）
同じ警報は ``repeat_s`` ごとにしか再発報しない（毎分 journal を埋めない）。消えたら ``resolved`` を 1 回出す。
heartbeat は ``heartbeat-<component>.json``。止まった監視を別の監視が見つけるための生存証拠（INV-80）。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

PRIORITY = {"crit": 2, "warn": 4, "info": 6}
_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class Alert:
    key: str
    level: str  # crit / warn / info
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


def default_dir() -> Path:
    home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(home) / "avp-host-monitor"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)  # 途中で壊れたファイルを残さない


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class AlertSink:
    def __init__(
        self,
        directory: Path,
        *,
        now: float,
        repeat_s: int = 3600,
        stream: TextIO | None = None,
    ) -> None:
        self.dir = directory
        self.now = now
        self.repeat_s = repeat_s
        self.stream = stream if stream is not None else sys.stderr

    @property
    def current_path(self) -> Path:
        return self.dir / "alerts-current.json"

    def _emit(self, source: str, kind: str, alert: Alert, level: str) -> None:
        line = (
            f"<{PRIORITY.get(level, 6)}>avp-alert {kind} source={source} key={alert.key} "
            f"level={level} msg={alert.message}"
        )
        print(line, file=self.stream)
        record = {
            "at": self.now,
            "kind": kind,
            "source": source,
            "key": alert.key,
            "level": level,
            "message": alert.message,
            "detail": alert.detail,
        }
        with contextlib.suppress(OSError):  # 履歴が書けなくても journal には出ている
            self.dir.mkdir(parents=True, exist_ok=True)
            with (self.dir / "alerts.jsonl").open("a") as f:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def sync(self, source: str, active: Mapping[str, Alert]) -> list[str]:
        """``source`` の有効な警報の集合を反映する。発報した key（new / repeat / resolved）を返す。"""
        data = _read_json(self.current_path)
        everything: dict[str, dict[str, dict[str, Any]]] = data if isinstance(data, dict) else {}
        before = everything.get(source, {})
        after: dict[str, dict[str, Any]] = {}
        emitted: list[str] = []
        for key, alert in active.items():
            prev = before.get(key)
            first = float(prev["since"]) if prev else self.now
            last_emit = float(prev["emitted_at"]) if prev else None
            due = last_emit is None or self.now - last_emit >= self.repeat_s
            if due or (prev is not None and prev.get("level") != alert.level):
                self._emit(source, "alert" if prev is None else "repeat", alert, alert.level)
                last_emit = self.now
                emitted.append(key)
            after[key] = {
                "since": first,
                "emitted_at": last_emit,
                "level": alert.level,
                "message": alert.message,
                "detail": alert.detail,
            }
        for key, prev in before.items():
            if key not in active:
                self._emit(
                    source, "resolved", Alert(key, "info", str(prev.get("message", ""))), "info"
                )
                emitted.append(key)
        if after:
            everything[source] = after
        else:
            everything.pop(source, None)
        with contextlib.suppress(OSError):
            _atomic_write(
                self.current_path, json.dumps(everything, ensure_ascii=False, sort_keys=True)
            )
        return emitted


def current_alerts(directory: Path) -> dict[str, dict[str, dict[str, Any]]]:
    data = _read_json(directory / "alerts-current.json")
    return data if isinstance(data, dict) else {}


def write_heartbeat(
    directory: Path, component: str, *, now: float, level: str = "ok", **extra: Any
) -> None:
    if not _COMPONENT.match(component):
        raise ValueError(f"bad component name: {component!r}")
    payload = {"component": component, "at": now, "level": level, **extra}
    with contextlib.suppress(OSError):  # 書けない = watcher が stale として検知する
        _atomic_write(
            directory / f"heartbeat-{component}.json", json.dumps(payload, sort_keys=True)
        )


def heartbeat_age(directory: Path, component: str, *, now: float) -> float | None:
    """heartbeat からの経過秒。無い・壊れている場合は None（= 生きている証拠が無い）。"""
    data = _read_json(directory / f"heartbeat-{component}.json")
    try:
        return now - float(data["at"])
    except (TypeError, KeyError, ValueError):
        return None
