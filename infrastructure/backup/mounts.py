"""コンテナの mount 情報（``docker inspect``。読み取りだけ）。run と precheck で共有する。"""

from __future__ import annotations

import json
from typing import Any

from infrastructure.backup.runner import CommandRunner


def inspect_json(runner: CommandRunner, container: str, template: str) -> Any | None:
    """``docker inspect --format '{{json <template>}}'`` の結果。失敗・不正な JSON は None。"""
    res = runner.run(["docker", "inspect", "--format", "{{json " + template + "}}", container])
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except ValueError:
        return None


def mount_source(runner: CommandRunner, container: str, destination: str) -> str | None:
    """コンテナ内 ``destination`` の mount 元（host path）。確認できなければ None。"""
    mounts = inspect_json(runner, container, ".Mounts")
    if not isinstance(mounts, list):
        return None
    for mount in mounts:
        if mount.get("Destination") == destination and mount.get("Source"):
            return str(mount["Source"])
    return None


def env_names(runner: CommandRunner, container: str) -> list[str] | None:
    """コンテナの環境変数の**名前だけ**。値はここで捨てて、呼び出し側へ渡さない。"""
    env = inspect_json(runner, container, ".Config.Env")
    if not isinstance(env, list):
        return None
    return sorted({str(item).split("=", 1)[0] for item in env})
