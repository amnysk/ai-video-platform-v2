"""compose.yaml の healthcheck から、常駐 worker が poll すべき task queue を導く。

queue 名の定義元は compose の healthcheck（``poller_check --queue``）で、ここはそれを読むだけ
（AGENTS.md §8: 別の一覧を書き直さない）。
一致は tests/contract/test_release_queues.py が yaml で照合する。
"""

from __future__ import annotations

import re

DEFAULT_MAX_AGE_SECONDS = 120.0  # poller_check の既定と同じ
_HEALTH = re.compile(r'poller_check"((?:,\s*"[^"]*")+)')


def required_queues(compose_text: str) -> dict[str, float]:
    """queue 名 -> poller の最終アクセスの許容秒数。"""
    queues: dict[str, float] = {}
    for line in compose_text.splitlines():
        match = _HEALTH.search(line)
        if not match:
            continue
        args = re.findall(r'"([^"]*)"', match.group(1))
        max_age = DEFAULT_MAX_AGE_SECONDS
        for i, arg in enumerate(args[:-1]):
            if arg == "--max-age-seconds":
                max_age = float(args[i + 1])
        for i, arg in enumerate(args[:-1]):
            if arg == "--queue":
                queues[args[i + 1]] = max_age
    return queues
