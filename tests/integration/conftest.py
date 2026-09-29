from __future__ import annotations

import os

import pytest

# Temporal namespace / MinIO bucket の隔離（ADR-0021 追補）: 本番へ向いていたら
# 収集時点で止める（skip ではなく例外）。TEMPORAL_ADDRESS / MINIO_ENDPOINT が
# 未設定の環境（インフラ無しで unit だけ走らせる等）では検査しない。
if os.environ.get("TEMPORAL_ADDRESS"):
    from tests.support.temporal import require_test_temporal_namespace

    require_test_temporal_namespace()
if os.environ.get("MINIO_ENDPOINT"):
    from tests.support.minio import require_test_minio_bucket

    require_test_minio_bucket()


def pytest_collection_modifyitems(items) -> None:
    """tests/integration 配下は全て integration マーク扱いにする。"""
    for item in items:
        if "tests/integration/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.integration)
