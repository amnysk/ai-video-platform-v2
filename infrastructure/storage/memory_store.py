"""インメモリ実装。テストとローカル実験用。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.storage.artifact_store import (
    ArtifactConflictError,
    ObjectStat,
    PutResult,
    read_bytes_source,
)


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._writes: dict[str, int] = {}
        self._content_types: dict[str, str] = {}

    async def put_json(self, key: str, payload: Mapping[str, Any]) -> PutResult:
        return self._put(key, canonical_json_bytes(payload))

    def _put(self, key: str, body: bytes) -> PutResult:
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

    async def put_text(self, key: str, body: str) -> PutResult:
        return self._put(key, body.encode("utf-8"))

    async def get_text(self, key: str) -> str:
        return self._objects[key].decode("utf-8")

    async def put_bytes(self, key: str, data: bytes | Path, content_type: str) -> PutResult:
        body = read_bytes_source(data)
        result = self._put(key, body)
        self._content_types.setdefault(key, content_type)
        return result

    async def get_bytes(self, key: str) -> bytes:
        return self._objects[key]

    async def sha256_of(self, key: str) -> str:
        return sha256_hex(self._objects[key])

    async def download_to(self, key: str, path: Path) -> str:
        body = self._objects[key]
        path.write_bytes(body)
        return sha256_hex(body)

    async def put_file(self, key: str, path: Path, content_type: str) -> PutResult:
        return await self.put_bytes(key, path, content_type)

    async def stat(self, key: str) -> ObjectStat:
        body = self._objects[key]
        return ObjectStat(
            size=len(body),
            etag=hashlib.md5(body, usedforsecurity=False).hexdigest(),
            content_type=self._content_types.get(key),
        )

    async def exists(self, key: str) -> bool:
        return key in self._objects

    def write_count(self, key: str) -> int:
        """そのキーへ実際に書き込んだ回数。immutability検査に使う。"""
        return self._writes.get(key, 0)
