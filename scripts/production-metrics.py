#!/usr/bin/env python
"""制作の拒否率・代替案・再試行・1本あたり費用・完成率の集計（ADR-0035）。**読み取り専用**。

    python scripts/production-metrics.py                  # 全期間
    python scripts/production-metrics.py --since 2026-09-20
    python scripts/production-metrics.py --json

SELECT だけを実行する（PostgreSQL ではトランザクションを READ ONLY にする）。provider・
Temporal・MinIO には触れない。費用は ``provider_reservations.estimated_cost_usd`` の見積りで、
provider の請求額ではない（拒否時の課金有無は provider が文書化していない）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

PAID_PROVIDERS = ("fal_image", "fal_video")


async def collect(session: AsyncSession, since: datetime | None = None) -> dict[str, Any]:
    """集計の本体（試験から直接呼ぶ）。``since`` は Episode の作成時刻での絞り込み。"""
    params = {"since": since or datetime(1970, 1, 1)}
    episodes_in_scope = "SELECT id FROM episodes WHERE created_at >= :since"

    providers: dict[str, dict[str, Any]] = {}
    for provider in PAID_PROVIDERS:
        row = (
            await session.execute(
                text(
                    "SELECT count(*) AS submitted, "
                    "coalesce(sum(CASE WHEN round > 1 THEN 1 ELSE 0 END), 0) AS later_rounds "
                    "FROM provider_reservations "
                    f"WHERE provider = :provider AND episode_id IN ({episodes_in_scope}) "
                    "AND dispatched_at IS NOT NULL"
                ),
                {**params, "provider": provider},
            )
        ).one()
        rejected = (
            await session.execute(
                text(
                    "SELECT count(*) FROM provider_rejections "
                    f"WHERE provider = :provider AND episode_id IN ({episodes_in_scope})"
                ),
                {**params, "provider": provider},
            )
        ).scalar_one()
        submitted = int(row.submitted)
        providers[provider] = {
            "submitted": submitted,
            "rejected": int(rejected),
            "rejection_rate": round(int(rejected) / submitted, 4) if submitted else None,
            "later_round_submits": int(row.later_rounds),
        }

    episodes = (
        await session.execute(
            text(
                "SELECT count(*) AS total, "
                "coalesce(sum(CASE WHEN status = 'uploaded' THEN 1 ELSE 0 END), 0) AS uploaded, "
                "coalesce(sum(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END), 0) AS blocked "
                "FROM episodes WHERE created_at >= :since"
            ),
            params,
        )
    ).one()
    alternatives = (
        await session.execute(
            text(
                "SELECT count(*) AS total, count(DISTINCT episode_id) AS episodes "
                "FROM artifact_metadata WHERE artifact_type = 'scene_visual_override' "
                f"AND episode_id IN ({episodes_in_scope})"
            ),
            params,
        )
    ).one()
    cost_rows = (
        await session.execute(
            text(
                "SELECT r.episode_id AS episode_id, e.status AS status, "
                "coalesce(sum(r.estimated_cost_usd), 0) AS cost "
                "FROM provider_reservations r JOIN episodes e ON e.id = r.episode_id "
                "WHERE r.provider IN ('fal_image', 'fal_video') AND e.created_at >= :since "
                "GROUP BY r.episode_id, e.status"
            ),
            params,
        )
    ).all()
    costs = [Decimal(str(r.cost)) for r in cost_rows]
    uploaded_costs = [Decimal(str(r.cost)) for r in cost_rows if r.status == "uploaded"]
    total = int(episodes.total)
    return {
        "since": since.isoformat() if since else None,
        "providers": providers,
        "scene_alternatives": {
            "total": int(alternatives.total),
            "episodes_with_alternatives": int(alternatives.episodes),
        },
        "episodes": {
            "total": total,
            "uploaded": int(episodes.uploaded),
            "blocked": int(episodes.blocked),
            "completion_rate": round(int(episodes.uploaded) / total, 4) if total else None,
        },
        "estimated_cost_usd": {
            "total": str(sum(costs, Decimal("0")).quantize(Decimal("0.0001"))),
            "per_episode_with_paid_calls": (
                str((sum(costs, Decimal("0")) / len(costs)).quantize(Decimal("0.0001")))
                if costs
                else None
            ),
            "per_uploaded_episode": (
                str(
                    (sum(uploaded_costs, Decimal("0")) / len(uploaded_costs)).quantize(
                        Decimal("0.0001")
                    )
                )
                if uploaded_costs
                else None
            ),
        },
    }


def _print(report: dict[str, Any]) -> None:
    print(f"since: {report['since'] or 'all time'}")
    for provider, stats in report["providers"].items():
        rate = stats["rejection_rate"]
        print(
            f"{provider}: submitted={stats['submitted']} rejected={stats['rejected']} "
            f"rejection_rate={'n/a' if rate is None else f'{rate:.2%}'} "
            f"later_round_submits={stats['later_round_submits']}"
        )
    alt = report["scene_alternatives"]
    print(f"scene alternatives: {alt['total']} in {alt['episodes_with_alternatives']} episode(s)")
    ep = report["episodes"]
    rate = ep["completion_rate"]
    print(
        f"episodes: total={ep['total']} uploaded={ep['uploaded']} blocked={ep['blocked']} "
        f"completion_rate={'n/a' if rate is None else f'{rate:.2%}'}"
    )
    cost = report["estimated_cost_usd"]
    print(
        f"estimated cost (USD): total={cost['total']} "
        f"per_episode={cost['per_episode_with_paid_calls']} "
        f"per_uploaded_episode={cost['per_uploaded_episode']}"
    )


async def main(argv: list[str]) -> int:
    from infrastructure.config import Settings

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", type=date.fromisoformat, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    since = datetime.combine(args.since, time.min) if args.since else None

    engine = create_async_engine(Settings().database_url)
    try:
        async with AsyncSession(engine) as session:
            if engine.dialect.name == "postgresql":
                await session.execute(text("SET TRANSACTION READ ONLY"))
            report = await collect(session, since)
            await session.rollback()
    finally:
        await engine.dispose()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
