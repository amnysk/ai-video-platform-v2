#!/usr/bin/env python
"""Production worker が使う provider 資格情報の preflight（ADR-0030）。

    python scripts/production-preflight.py

ロジックは ``infrastructure/production/preflight.py``（ユニットテスト対象）。このスクリプトは
CLI の薄い皮だけを持つ。

各チェックは「検証済み」（OK/FAIL、実際に確認した）と「未検証」（UNVERIFIED、確認していない／
できない）を明確に分けて表示する。未検証の項目は healthy として扱わない。

fal の ``/storage/auth/token`` エンドポイントが非課金であることは、本スクリプト作成時点で
fal の公式価格ドキュメントから断定できなかった。断定できないため、既定では実呼び出しをしない。
運用者が自分の判断で fal に確認した上でのみ ``AVP_PREFLIGHT_CONFIRMED_NONBILLING=1`` を設定して
実呼び出しを有効にできる（AGENTS.md §9: 所有者の明示的な判断だけが変えられる）。

終了コード: 0 = 検証済みの全チェックが healthy / 1 = いずれかが unhealthy。
「未検証」は失敗として数えない。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from infrastructure.config import Settings  # noqa: E402
from infrastructure.production.preflight import check_fal_credentials  # noqa: E402


async def main() -> int:
    settings = Settings()
    checks = await check_fal_credentials(settings)
    for check in checks:
        print(f"[{check.status:^10}] {check.name}: {check.detail}")
    print()
    if any(check.is_failure for check in checks):
        print("NG: at least one verified check failed.")
        return 1
    print("OK (with UNVERIFIED items above, if any): no verified check failed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
