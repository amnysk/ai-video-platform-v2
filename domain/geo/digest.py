"""地図の証跡で使う正準 JSON の sha256（ADR-0084、INV-141）。純関数。

形式は ``domain.artifact.hashing.canonical_json_bytes`` と同じ（キー順に依存しない・非 ASCII を
エスケープしない・区切りは詰める）。``domain.geo`` は ``domain.artifact`` を import できない
（INV-6）ので同じ形式をここに持つ。``RenderedMap.manifest_sha256`` と一致することは
``tests/unit/test_geo_digest.py`` が検査する。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256_of(payload: Mapping[str, Any] | BaseModel) -> str:
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    return hashlib.sha256(canonical_bytes(data)).hexdigest()


__all__ = ["canonical_bytes", "sha256_of"]
