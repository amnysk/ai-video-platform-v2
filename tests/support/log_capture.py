"""テストで実際の JSON 整形器の出力を捕まえる（root に handler を一時的に足す）。"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from infrastructure.logging.formatter import JsonFormatter, ServiceIdentity


class Captured:
    def __init__(self, stream: io.StringIO) -> None:
        self.stream = stream

    def events(self, event_name: str | None = None) -> list[dict[str, Any]]:
        out = [json.loads(line) for line in self.stream.getvalue().splitlines() if line]
        if event_name is None:
            return out
        return [e for e in out if e["event_name"] == event_name]

    def names(self) -> list[str]:
        """業務イベントの名前（``log.record``＝既存・第三者 logger の記録は除く）。"""
        return [e["event_name"] for e in self.events() if e["event_name"] != "log.record"]


@contextmanager
def capture_json(level: int = logging.DEBUG) -> Iterator[Captured]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(ServiceIdentity("tests", "test", "test")))
    root = logging.getLogger()
    saved = root.level
    root.addHandler(handler)
    root.setLevel(level)
    try:
        yield Captured(stream)
    finally:
        root.removeHandler(handler)
        root.setLevel(saved)
