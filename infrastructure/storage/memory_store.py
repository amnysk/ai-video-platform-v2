"""インメモリ実装。テストとローカル実験用。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.storage.artifact_store import ArtifactConflictError, PutResult


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._writes: dict[str, int] = {}

    async def put_json(self, key: str, payload: Mapping[str, Any]) -> PutResult:
        body = canonical_json_bytes(payload)
        digest = sha256_hex(body)
        existing = self._objects.get(key)
        if existing is not None:
            if existing != body:
                raise ArtifactConflictError(
                    f"artifact already exists with different content: {key}"
                )
            return PutResult(key=key, sha256=digest, size=len(body), existed=True)

        self._objects[key] = body
        self._writes[key] = self._writes.get(key, 0) + 1
        return PutResult(key=key, sha256=digest, size=len(body), existed=False)

    async def get_json(self, key: str) -> dict[str, Any]:
        body = self._objects[key]
        return json.loads(body.decode("utf-8"))

    async def exists(self, key: str) -> bool:
        return key in self._objects

    def write_count(self, key: str) -> int:
        """そのキーへ実際に書き込んだ回数。immutability検査に使う。"""
        return self._writes.get(key, 0)
