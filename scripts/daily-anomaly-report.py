#!/usr/bin/env python
"""日次運用の異常を窓（24h/7d/30d/全期間）と重複除去つきで集計する。**読み取り専用**。

    python scripts/daily-anomaly-report.py
    python scripts/daily-anomaly-report.py --json
    python scripts/daily-anomaly-report.py --temporal-url http://localhost:8233   # 既定
    python scripts/daily-anomaly-report.py --no-temporal                   # Temporal を読まない

PostgreSQL は ``READ ONLY`` トランザクションで SELECT だけ、Temporal は UI の HTTP API を
GET するだけ。OpenSearch・provider・MinIO・Docker には触れない
（OpenSearch の検索条件は ``deploy/logging/queries/``）。
終了コード: 0 = いま再発中の未解決 incident が無い / 1 = ある / 2 = 実行できなかった。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from domain.anomaly_report import render_text, total_recurring_unresolved  # noqa: E402
from infrastructure.config import Settings  # noqa: E402
from infrastructure.diagnostics.anomaly_events import (  # noqa: E402
    TemporalHttpReader,
    collect_report,
)


async def _main(args: argparse.Namespace) -> int:
    reader = None if args.no_temporal else TemporalHttpReader(base_url=args.temporal_url)
    engine = create_async_engine(Settings().database_url)
    try:
        async with AsyncSession(engine) as session:
            report = await collect_report(session, datetime.now(UTC), reader)
            await session.rollback()
    finally:
        await engine.dispose()
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_text(report))
    return 1 if total_recurring_unresolved(report) else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--json", action="store_true")
    p.add_argument("--temporal-url", default="http://localhost:8233")
    p.add_argument("--no-temporal", action="store_true")
    try:
        return asyncio.run(_main(p.parse_args()))
    except Exception as exc:
        print(f"daily-anomaly-report failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
