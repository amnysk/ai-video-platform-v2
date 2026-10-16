"""integration テストが使う MinIO bucket の隔離（ADR-0021 追補）。

本番 worker は ``Settings().minio_bucket``（既定 ``artifacts``）を使う。テストが同じ bucket を
使うと、テストが作った episode 由来のオブジェクトが本番の bucket に紛れ込む。

**実測（2026-09-23）**: 本番の ``artifacts`` bucket には 869 個の episode-id フォルダがあったが、
本番 PostgreSQL の ``episodes`` 行は 35 件しかなかった。差分の約834件は、この隔離が無いまま
ローカルで実行された過去の integration テストが残したものとほぼ断定できる（content-addressed
キーなので実データの破損ではないが、実害の無い汚染ではない）。そのため:

- テストが使う bucket は ``TEST_MINIO_BUCKET``（既定 ``artifacts-test``）**だけ**
- アプリの bucket（``MINIO_BUCKET`` / ``Settings().minio_bucket``）と同じ名前は拒否する
- 拒否は skip ではなく**例外**（``tests/integration/conftest.py`` が収集時に呼ぶ）
- bucket が無ければ ``MinioArtifactStore.ensure_bucket`` が作る（endpoint/credentials は
  ``Settings()`` のまま、bucket 名だけ上書きする。client 構築を複製しない）

本番 bucket に残った汚染物の削除はこのモジュールの責務ではない（自動削除は事故の温床。
既存物の棚卸しと削除は運用者の判断）。
"""

from __future__ import annotations

import os
from collections.abc import Iterable

DEFAULT_TEST_BUCKET = "artifacts-test"
ENV_TEST_BUCKET = "TEST_MINIO_BUCKET"


class UnsafeTestMinioBucketError(RuntimeError):
    """テスト用として安全と確認できない MinIO bucket。"""


def validate_test_minio_bucket(bucket: str, *, forbidden: Iterable[str | None]) -> str:
    """安全なテスト用 bucket ならそのまま返し、そうでなければ例外。"""
    normalized = (bucket or "").strip()
    if not normalized:
        raise UnsafeTestMinioBucketError("テスト用 MinIO bucket が空")
    for other in forbidden:
        if other and other.strip().lower() == normalized.lower():
            raise UnsafeTestMinioBucketError(
                f"テスト用 MinIO bucket {normalized!r} がアプリの bucket と同じ"
            )
    return normalized


def _application_buckets() -> list[str | None]:
    from infrastructure.config import Settings

    return [os.environ.get("MINIO_BUCKET"), Settings().minio_bucket]


def require_test_minio_bucket() -> str:
    """``TEST_MINIO_BUCKET``（既定 ``artifacts-test``）を検証して返す。"""
    bucket = os.environ.get(ENV_TEST_BUCKET) or DEFAULT_TEST_BUCKET
    return validate_test_minio_bucket(bucket, forbidden=_application_buckets())


async def connect_test_artifact_store():
    """integration テストが使う ``MinioArtifactStore`` を、隔離済み bucket で作る。

    endpoint / access_key / secret_key はアプリの ``Settings()`` のまま
    （host/認証はテスト用に変える理由が無い。compose の同じ MinIO を使う）。
    bucket だけをテスト専用へ上書きする。client 構築ロジックは
    ``MinioArtifactStore.from_settings`` を再利用し、ここで複製しない。
    """
    from infrastructure.config import Settings
    from infrastructure.storage.minio_store import MinioArtifactStore

    settings = Settings(minio_bucket=require_test_minio_bucket())
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    return store


__all__ = [
    "DEFAULT_TEST_BUCKET",
    "ENV_TEST_BUCKET",
    "UnsafeTestMinioBucketError",
    "connect_test_artifact_store",
    "require_test_minio_bucket",
    "validate_test_minio_bucket",
]
