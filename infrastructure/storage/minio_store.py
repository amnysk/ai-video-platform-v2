"""MinIO実装（INV-9）。同期SDKをスレッドへ逃がしてイベントループを塞がない。"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error

from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.config import Settings
from infrastructure.storage.artifact_store import (
    ArtifactConflictError,
    ObjectStat,
    PutResult,
    read_bytes_source,
)

CONTENT_TYPE = "application/json"
TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"


class MinioArtifactStore:
    def __init__(self, client: Minio, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_settings(cls, settings: Settings) -> MinioArtifactStore:
        parsed = urlparse(settings.minio_endpoint)
        client = Minio(
            parsed.netloc or settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=parsed.scheme == "https",
        )
        return cls(client, settings.minio_bucket)

    @property
    def bucket(self) -> str:
        return self._bucket

    async def ensure_bucket(self) -> None:
        def _ensure() -> None:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)

        await asyncio.to_thread(_ensure)

    async def put_json(self, key: str, payload: Mapping[str, Any]) -> PutResult:
        return await self._put(key, canonical_json_bytes(payload), CONTENT_TYPE)

    async def put_text(self, key: str, body: str) -> PutResult:
        return await self._put(key, body.encode("utf-8"), TEXT_CONTENT_TYPE)

    async def get_text(self, key: str) -> str:
        body = await self._get_bytes(key)
        if body is None:
            raise KeyError(key)
        return body.decode("utf-8")

    async def put_bytes(self, key: str, data: bytes | Path, content_type: str) -> PutResult:
        return await self._put(key, read_bytes_source(data), content_type)

    async def get_bytes(self, key: str) -> bytes:
        body = await self._get_bytes(key)
        if body is None:
            raise KeyError(key)
        return body

    async def stat(self, key: str) -> ObjectStat:
        def _stat() -> ObjectStat:
            try:
                info = self._client.stat_object(self._bucket, key)
            except S3Error as err:
                if err.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                    raise KeyError(key) from err
                raise
            return ObjectStat(
                size=int(info.size or 0),
                etag=str(info.etag or ""),
                content_type=info.content_type,
            )

        return await asyncio.to_thread(_stat)

    async def _put(self, key: str, body: bytes, content_type: str) -> PutResult:
        digest = sha256_hex(body)

        existing = await self._get_bytes(key)
        if existing is not None:
            if existing != body:
                raise ArtifactConflictError(
                    f"artifact already exists with different content: {key}"
                )
            return PutResult(key=key, sha256=digest, size=len(body), existed=True)

        def _put() -> None:
            self._client.put_object(
                self._bucket,
                key,
                io.BytesIO(body),
                length=len(body),
                content_type=content_type,
            )

        await asyncio.to_thread(_put)
        return PutResult(key=key, sha256=digest, size=len(body), existed=False)

    async def get_json(self, key: str) -> dict[str, Any]:
        body = await self._get_bytes(key)
        if body is None:
            raise KeyError(key)
        return json.loads(body.decode("utf-8"))

    async def exists(self, key: str) -> bool:
        def _stat() -> bool:
            try:
                self._client.stat_object(self._bucket, key)
            except S3Error as err:
                if err.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                    return False
                raise
            return True

        return await asyncio.to_thread(_stat)

    async def _get_bytes(self, key: str) -> bytes | None:
        def _get() -> bytes | None:
            response = None
            try:
                response = self._client.get_object(self._bucket, key)
                return response.read()
            except S3Error as err:
                if err.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                    return None
                raise
            finally:
                if response is not None:
                    response.close()
                    response.release_conn()

        return await asyncio.to_thread(_get)
