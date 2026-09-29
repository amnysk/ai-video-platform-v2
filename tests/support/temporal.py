"""integration テストが使う Temporal 接続と、namespace 隔離の安全装置（ADR-0021 追補）。

本番の worker は ``Settings().temporal_namespace``（既定 ``default``）を poll している。
テストが同じ namespace に workflow を起動すると、task queue を分けても本番の namespace に
テスト由来の workflow が残り、本番 worker・Schedule・運用者の一覧と混ざる。そのため:

- テストが使う namespace は ``TEST_TEMPORAL_NAMESPACE``（既定 ``avp-test``）**だけ**
- ``default`` と、アプリの namespace（``TEMPORAL_NAMESPACE`` / ``Settings().temporal_namespace``）は
  拒否する。拒否は skip ではなく**例外**（``tests/integration/conftest.py`` が収集時に呼ぶ）
- integration テストの Temporal client は :func:`connect_test_client` からだけ作る
  （``tests/architecture/test_integration_temporal_client.py`` が検査）
- 実運用: ``docker compose exec avp2-minio-1`` の隣接調査で、本番 MinIO bucket に
  テスト由来と見られるオブジェクトが大量に混入していたことが確認されている
  （``tests/support/minio.py`` 参照）。Temporal namespace でも同じ事故が起きうる前提で、
  「明示的に安全と確認できたときだけ実行する」を既定にする。

namespace は ``make temporal-test-namespace`` で作る（冪等）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable

from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client

DEFAULT_TEST_NAMESPACE = "avp-test"
ENV_TEST_NAMESPACE = "TEST_TEMPORAL_NAMESPACE"
PRODUCTION_DEFAULT_NAMESPACE = "default"


class UnsafeTestTemporalNamespaceError(RuntimeError):
    """テスト用として安全と確認できない Temporal namespace。"""


def validate_test_temporal_namespace(namespace: str, *, forbidden: Iterable[str | None]) -> str:
    """安全なテスト用 namespace ならそのまま返し、そうでなければ例外。"""
    normalized = (namespace or "").strip()
    if not normalized:
        raise UnsafeTestTemporalNamespaceError("テスト用 Temporal namespace が空")
    if normalized.lower() == PRODUCTION_DEFAULT_NAMESPACE:
        raise UnsafeTestTemporalNamespaceError(
            "テスト用 Temporal namespace に 'default'（本番 worker の namespace）は使えない"
        )
    for other in forbidden:
        if other and other.strip().lower() == normalized.lower():
            raise UnsafeTestTemporalNamespaceError(
                f"テスト用 Temporal namespace {normalized!r} がアプリの namespace と同じ"
            )
    return normalized


def _application_namespaces() -> list[str | None]:
    from infrastructure.config import Settings

    return [os.environ.get("TEMPORAL_NAMESPACE"), Settings().temporal_namespace]


def require_test_temporal_namespace() -> str:
    """``TEST_TEMPORAL_NAMESPACE``（既定 ``avp-test``）を検証して返す。"""
    namespace = os.environ.get(ENV_TEST_NAMESPACE) or DEFAULT_TEST_NAMESPACE
    return validate_test_temporal_namespace(namespace, forbidden=_application_namespaces())


def temporal_test_address() -> str | None:
    """integration テストが繋ぐ Temporal（未設定なら ``None`` = 呼び出し側で skip）。"""
    return os.environ.get("TEMPORAL_ADDRESS") or None


def worker_subprocess_env() -> dict[str, str]:
    """テスト用 worker サブプロセスへ渡す Temporal 接続先（namespace を含む）。"""
    return {
        "TEMPORAL_ADDRESS": temporal_test_address() or "",
        ENV_TEST_NAMESPACE: require_test_temporal_namespace(),
    }


async def connect_test_client(address: str | None = None, *, timeout: float = 5.0) -> Client:
    """テスト専用 namespace へ接続した Temporal client。

    namespace が無ければ作り方を示して失敗する（黙って ``default`` へ落ちない）。
    """
    target = address or temporal_test_address()
    if not target:
        raise RuntimeError("TEMPORAL_ADDRESS が未設定")
    namespace = require_test_temporal_namespace()
    client = await asyncio.wait_for(Client.connect(target, namespace=namespace), timeout=timeout)
    try:
        await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=namespace)
        )
    except Exception as exc:
        raise RuntimeError(
            f"Temporal namespace {namespace!r} が無い。`make temporal-test-namespace` で作る"
        ) from exc
    return client


__all__ = [
    "DEFAULT_TEST_NAMESPACE",
    "ENV_TEST_NAMESPACE",
    "PRODUCTION_DEFAULT_NAMESPACE",
    "UnsafeTestTemporalNamespaceError",
    "connect_test_client",
    "require_test_temporal_namespace",
    "temporal_test_address",
    "validate_test_temporal_namespace",
    "worker_subprocess_env",
]
