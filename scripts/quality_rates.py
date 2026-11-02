#!/usr/bin/env python
"""品質ゲートの判定率の集計（ADR-0087 §11）。**読み取り専用**。

    python scripts/quality_rates.py --database-url postgresql+asyncpg://... [--since 2026-11-01]

``quality_reports`` を SELECT するだけ（PostgreSQL ではトランザクションの先頭で
``SET TRANSACTION READ ONLY``。``BEGIN READ ONLY`` と同じ効果）。provider・Temporal・MinIO・
YouTube には触れない。DB の URL は**必ず明示**する（既定値は無い。本番を誤って読まないため）。

JSON を標準出力へ出す:
  - ``reports``: 全報告に対する PASS / WARN / REVIEW / BLOCK / inspection_unavailable の件数と率、
    投稿可能率（PASS + WARN）
  - ``latest_per_episode``: Episode ごとに最新の報告 1 件だけを数えた同じ集計
  - ``recent_hold_alarm``: 直近 10 件中 5 件以上が保留かどうか（内訳つき）

``inspection_unavailable`` は検査系の故障による保留（INV-156）で、REVIEW の率に混ぜない。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import datetime
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from contracts.quality import QualityVerdict  # noqa: E402
from domain.quality.hold_rate import (  # noqa: E402
    GateDecisionSample,
    decision_rates,
    recent_hold_alarm,
    sample_from_report,
)


def _samples(rows: list[Any]) -> list[GateDecisionSample]:
    out: list[GateDecisionSample] = []
    for verdict, report_json in rows:
        payload = json.loads(report_json) if isinstance(report_json, str) else report_json
        codes = [str(c.get("reason_code", "")) for c in (payload or {}).get("checks", [])]
        out.append(sample_from_report(QualityVerdict(verdict), codes))
    return out


async def collect(session: AsyncSession, since: datetime | None = None) -> dict[str, Any]:
    """集計の本体（試験から直接呼ぶ）。SELECT だけ。古い順に並べる。"""
    params = {"since": since or datetime(1970, 1, 1)}
    rows = (
        await session.execute(
            text(
                "SELECT episode_id, verdict, report_json FROM quality_reports "
                "WHERE generated_at >= :since ORDER BY generated_at ASC, created_at ASC"
            ),
            params,
        )
    ).all()
    all_samples = _samples([(r[1], r[2]) for r in rows])
    latest: dict[str, tuple[Any, Any]] = {}
    for episode_id, verdict, report_json in rows:  # 古い順なので後ろが勝つ
        latest[str(episode_id)] = (verdict, report_json)
    alarm = recent_hold_alarm(all_samples) if len(all_samples) >= 5 else None
    return {
        "since": since.isoformat() if since else None,
        "reports": decision_rates(all_samples).to_dict(),
        "latest_per_episode": decision_rates(_samples(list(latest.values()))).to_dict(),
        "recent_hold_alarm": alarm.to_detail() if alarm else None,
    }


def normalize_url(url: str) -> str:
    """同期ドライバの URL でも非同期ドライバに直す（読み取りだけなので意味は変わらない）。"""
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("sqlite://") and not url.startswith("sqlite+aiosqlite://"):
        return "sqlite+aiosqlite://" + url[len("sqlite://") :]
    return url


async def run(url: str, since: datetime | None = None) -> dict[str, Any]:
    engine = create_async_engine(normalize_url(url))
    try:
        async with AsyncSession(engine) as session:
            if engine.dialect.name == "postgresql":
                await session.execute(text("SET TRANSACTION READ ONLY"))
            report = await collect(session, since)
            await session.rollback()
    finally:
        await engine.dispose()
    return report


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--since", type=datetime.fromisoformat, default=None)
    args = parser.parse_args(argv)
    print(json.dumps(await run(args.database_url, args.since), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
