"""integration テストの MinIO bucket 隔離（ADR-0021 追補）。

テストが本番 worker と同じ bucket（既定 ``artifacts``）へオブジェクトを書かないよう、
``TEST_MINIO_BUCKET``（既定 ``artifacts-test``）だけを使い、アプリの bucket は収集時に拒否する。
"""

from __future__ import annotations

import pytest

from tests.support.minio import (
    DEFAULT_TEST_BUCKET,
    UnsafeTestMinioBucketError,
    require_test_minio_bucket,
    validate_test_minio_bucket,
)


def test_default_is_artifacts_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_MINIO_BUCKET", raising=False)
    monkeypatch.delenv("MINIO_BUCKET", raising=False)
    assert DEFAULT_TEST_BUCKET == "artifacts-test"
    assert require_test_minio_bucket() == "artifacts-test"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_MINIO_BUCKET", "ci-artifacts-test")
    monkeypatch.delenv("MINIO_BUCKET", raising=False)
    assert require_test_minio_bucket() == "ci-artifacts-test"


@pytest.mark.parametrize("bucket", ["", "  "])
def test_rejects_empty(bucket: str) -> None:
    with pytest.raises(UnsafeTestMinioBucketError):
        validate_test_minio_bucket(bucket, forbidden=())


def test_rejects_the_application_bucket() -> None:
    with pytest.raises(UnsafeTestMinioBucketError):
        validate_test_minio_bucket("artifacts", forbidden=["artifacts", None])


def test_rejects_case_insensitive_match() -> None:
    with pytest.raises(UnsafeTestMinioBucketError):
        validate_test_minio_bucket("Artifacts", forbidden=["artifacts"])


def test_env_pointing_at_the_application_bucket_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIO_BUCKET", "prod-bucket")
    monkeypatch.setenv("TEST_MINIO_BUCKET", "prod-bucket")
    with pytest.raises(UnsafeTestMinioBucketError):
        require_test_minio_bucket()
