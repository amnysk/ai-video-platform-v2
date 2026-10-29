from __future__ import annotations

import os

import pytest

# 本番スタックへの誤接続（ADR-0079 / INV-121）: 接続先が1つでも設定されているなら、隔離の宣言
# （run-e2e.sh の apptest / CI の ci-ephemeral）が無い限り収集時点で例外にする（skip にしない）。
# 他の guard（namespace・bucket・DB 名）は「本番サーバの中の別名前空間」しか見られない
from tests.support.production_guard import assert_isolated_targets  # noqa: E402

assert_isolated_targets(os.environ)
if os.environ.get("TEMPORAL_ADDRESS"):
    import asyncio

    from tests.support.temporal import assert_temporal_is_not_production

    asyncio.run(assert_temporal_is_not_production(os.environ["TEMPORAL_ADDRESS"]))

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
