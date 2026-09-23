"""Production worker が使う provider 資格情報の preflight（ADR-0030）。

``scripts/production-preflight.py`` から呼ばれる。ロジックをここに置くのは、CLI スクリプト
（ファイル名にハイフンを含み、通常の import ができない）ではなくここでユニットテストするため。

worker（``workers/production_video/run_worker.py``）と**同じ** ``Settings`` 解決と**同じ**
``FalStorageClient`` 構築を使う（設定・クライアントを再実装しない）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from infrastructure.config import Settings
from infrastructure.providers.fal_storage import FalStorageClient

#: 運用者が fal に非課金であることを確認した上でだけ実呼び出しを有効にする（AGENTS.md §9）。
CONFIRMED_NONBILLING_ENV = "AVP_PREFLIGHT_CONFIRMED_NONBILLING"


class _StorageClientLike(Protocol):
    """``FalStorageClient`` のうちこのモジュールが使う部分だけ（テストで fake に差し替える）。"""

    def __init__(self, api_key: str, *, timeout_seconds: float) -> None: ...
    async def _token(self) -> tuple[str, str]: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    #: "OK" / "FAIL" / "UNVERIFIED"
    status: str
    detail: str
    #: "local"（プロセス外に一切出ない）/ "network"（実際に外部へ到達する）。
    #: 運用者が出力を見て「どれが実際に fal へ届くか」を一目で区別できるようにする。
    scope: str = "local"

    @property
    def is_failure(self) -> bool:
        return self.status == "FAIL"


async def check_fal_credentials(
    settings: Settings,
    *,
    client_factory: type[_StorageClientLike] = FalStorageClient,
    env: Mapping[str, str] | None = None,
) -> list[PreflightCheck]:
    """worker と同じ設定解決・同じクライアント構築で fal 資格情報を確認する。

    ``client_factory`` / ``env`` はテスト用の差し替え口（``__init__`` の生パラメータではなく
    型そのものを渡すのは ``FalStorageClient`` を素朴に fake へ差し替えられるようにするため）。
    """
    resolved_env: Mapping[str, str] = os.environ if env is None else env
    checks: list[PreflightCheck] = []

    if not settings.fal_key:
        checks.append(
            PreflightCheck(
                "fal_key configured",
                "FAIL",
                "FAL_KEY is not set (same check run_worker.py uses)",
            )
        )
        return checks
    checks.append(
        PreflightCheck("fal_key configured", "OK", "FAL_KEY is set (value not printed — INV-20)")
    )

    if resolved_env.get(CONFIRMED_NONBILLING_ENV) != "1":
        checks.append(
            PreflightCheck(
                "fal storage token endpoint reachable with these credentials",
                "UNVERIFIED",
                "fal's official docs do not explicitly state whether POST "
                "/storage/auth/token is billed. The pricing page scopes billing to "
                '"the output you generate" via Model APIs; the file-storage/CDN docs '
                "say nothing about cost either way. Not conclusive, so not making a "
                "live call by default (AGENTS.md §9). See docs/operations/"
                "production-preflight.md for the full evidence trail. Set "
                f"{CONFIRMED_NONBILLING_ENV}=1 after confirming with fal directly "
                "to enable this live check.",
                scope="local",
            )
        )
        return checks

    client = client_factory(
        settings.fal_key.get_secret_value(),
        timeout_seconds=settings.production_submit_timeout_seconds,
    )
    try:
        await client._token()  # noqa: SLF001 - worker と同じ内部呼び出しを使う
    except Exception as exc:  # ここは診断であって分類ではない。何であれ報告する
        checks.append(
            PreflightCheck(
                "fal storage token endpoint",
                "FAIL",
                f"{type(exc).__name__}: {exc}",
                scope="network",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                "fal storage token endpoint",
                "OK",
                "acquired a token with the configured FAL_KEY",
                scope="network",
            )
        )
    finally:
        await client.aclose()
    return checks


__all__ = ["CONFIRMED_NONBILLING_ENV", "PreflightCheck", "check_fal_credentials"]
