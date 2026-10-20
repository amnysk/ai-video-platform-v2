"""常駐しているべきアプリサービスの一覧を、既存の定義から導く（AGENTS.md §8: 別の一覧を書かない）。

- アプリサービス全体: ``scripts/deploy-workers.sh`` の ``APP_SERVICES``（deploy が作り直す集合）
- そのうち常駐しない one-shot（``restart: "no"``。migrate）: ``compose.yaml``

一致は tests/contract/test_release_app_services.py が yaml で照合する。
"""

from __future__ import annotations

import re

_ARRAY = re.compile(r"^APP_SERVICES=\(([^)]*)\)", re.MULTILINE)
_SERVICE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$")
_RESTART_NO = re.compile(r"""^    restart:\s*["']?no["']?\s*(?:#.*)?$""")


def app_services(deploy_script: str) -> list[str]:
    match = _ARRAY.search(deploy_script)
    return match.group(1).split() if match else []


def one_shot_services(compose_text: str) -> set[str]:
    """``services:`` 直下で ``restart: "no"`` のもの。"""
    in_services = False
    current: str | None = None
    found: set[str] = set()
    for line in compose_text.splitlines():
        if re.match(r"^\S", line):
            in_services = line.startswith("services:")
            current = None
            continue
        if not in_services:
            continue
        svc = _SERVICE.match(line)
        if svc:
            current = svc.group(1)
        elif current and _RESTART_NO.match(line):
            found.add(current)
    return found


def expected_running_services(deploy_script: str, compose_text: str) -> list[str]:
    one_shot = one_shot_services(compose_text)
    return [s for s in app_services(deploy_script) if s not in one_shot]
