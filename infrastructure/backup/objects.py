"""MinIO（S3 API）の読み取り。本番へは list / stat / get だけを呼ぶ。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error

from infrastructure.config import Settings

CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    etag: str


@dataclass(frozen=True)
class Downloaded:
    sha256: str
    md5: str
    size: int


class ObjectSource(Protocol):
    def bucket_exists(self, bucket: str) -> bool: ...

    def versioning(self, bucket: str) -> str: ...

    def list_objects(self, bucket: str) -> Iterator[ObjectInfo]: ...

    def download(self, bucket: str, key: str, dest: Path) -> Downloaded: ...


def is_plain_md5_etag(etag: str) -> bool:
    """multipart（``-N`` 付き）でない etag は本体の md5。"""
    e = etag.strip('"')
    return len(e) == 32 and all(c in "0123456789abcdef" for c in e)


class MinioObjectSource:
    def __init__(self, client: Minio) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> MinioObjectSource:
        parsed = urlparse(settings.minio_endpoint)
        return cls(
            Minio(
                parsed.netloc or settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=parsed.scheme == "https",
            )
        )

    def bucket_exists(self, bucket: str) -> bool:
        return bool(self._client.bucket_exists(bucket))

    def versioning(self, bucket: str) -> str:
        try:
            status = self._client.get_bucket_versioning(bucket).status
        except S3Error as err:
            return f"unknown:{err.code}"
        return str(status or "unversioned").lower()

    def list_objects(self, bucket: str) -> Iterator[ObjectInfo]:
        for obj in self._client.list_objects(bucket, recursive=True):
            if obj.is_dir or obj.object_name is None:
                continue
            yield ObjectInfo(obj.object_name, int(obj.size or 0), str(obj.etag or "").strip('"'))

    def download(self, bucket: str, key: str, dest: Path) -> Downloaded:
        sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
        response = self._client.get_object(bucket, key)
        try:
            with dest.open("xb") as out:
                for chunk in response.stream(CHUNK):
                    sha.update(chunk)
                    md5.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
        finally:
            response.close()
            response.release_conn()
        return Downloaded(sha.hexdigest(), md5.hexdigest(), size)
