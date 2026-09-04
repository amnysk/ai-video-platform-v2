"""Artifact本体の正規化とダイジェスト。純粋関数のみ。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """キー順に依存しない正準表現。

    同じ内容から必ず同じバイト列＝同じダイジェストが出ることが、
    content-addressedなキーと再開判定の前提になる。
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
