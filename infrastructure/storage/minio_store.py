"""MinIO実装（INV-9）。同期SDKをスレッドへ逃がしてイベントループを塞がない。"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error

from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.config import Settings
from infrastructure.storage.artifact_store import ArtifactConflictError, PutResult

CONTENT_TYPE = "application/json"


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
        body = canonical_json_bytes(payload)
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
                content_type=CONTENT_TYPE,
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
