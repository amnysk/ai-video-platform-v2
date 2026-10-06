"""API の起動点（ADR-0040 §1 / レビュー I-10）。理由は docs/testing/logging-rationale.md。

compose は api の command を ``python -m apps.api.serve`` に上書きするが、イメージの既定 CMD が
uvicorn の CLI のままだと、command を書かずに起動した時（手動の ``docker run`` 等）だけ
uvicorn の既定のログ設定（stderr・独自書式・query 付き access log）に戻る。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVE = ["python", "-m", "apps.api.serve"]


def _stage(dockerfile: str, name: str) -> str:
    body = re.split(rf"^FROM\s+\S+\s+AS\s+{name}\s*$", dockerfile, flags=re.M | re.I)[1]
    return re.split(r"^FROM\s", body, flags=re.M)[0]


def _last_cmd(*bodies: str) -> list[str] | None:
    found: list[str] | None = None
    for body in bodies:
        for match in re.finditer(r"^CMD\s+(\[.*\])\s*$", body, re.M):
            found = json.loads(match.group(1))
    return found


def test_the_app_image_defaults_to_the_logging_entry_point() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    # app stage は base を継承する。app に CMD が無ければ base の CMD が既定
    assert _last_cmd(_stage(dockerfile, "base"), _stage(dockerfile, "app")) == SERVE
    assert not re.search(r"^CMD\s+\[\"uvicorn\"", dockerfile, re.M)
