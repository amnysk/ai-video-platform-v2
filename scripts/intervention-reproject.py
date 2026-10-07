#!/usr/bin/env python
"""判断・介入イベント（``episode_events``、正本）をログへ再投影する（ADR-0041 §7）。

    python scripts/intervention-reproject.py              # 未投影（projected_at IS NULL）を全部
    python scripts/intervention-reproject.py --all        # 投影済みも含めて全部
    python scripts/intervention-reproject.py --episode <id> --from-seq 3 --to-seq 9
    python scripts/intervention-reproject.py --dry-run    # 数えるだけ（出さない・付けない）

DB を読んで stdout に JSON を出し、出した行に ``projected_at`` を付ける。
OpenSearch は読まない（INV-38）。出力を Collector に拾わせるには ``avp.logging=app`` の
label を持つ**新しいコンテナ**で走らせる（``docker compose run --rm <app service>
python scripts/intervention-reproject.py``。``docker exec`` の stdout はコンテナのログに
ならない）。手順は ``docs/operations/intervention-runbook.md`` §3。

同じ行は同じ ``event_id``（``uuid5(episode_id:seq)``）で出るので、何度走らせても同じ index の
中では重複しない。終了コード: 0 = 成功、2 = 引数の誤り。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from infrastructure.intervention.projection import reproject, validate_selection  # noqa: E402

logger = logging.getLogger("avp.intervention.reproject")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--episode", default=None, help="この Episode だけ")
    parser.add_argument("--from-seq", type=int, default=None)
    parser.add_argument("--to-seq", type=int, default=None)
    parser.add_argument(
        "--all", action="store_true", help="projected_at の付いた行も出し直す（既定は未投影だけ）"
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        validate_selection(episode_id=args.episode, from_seq=args.from_seq, to_seq=args.to_seq)
    except ValueError as exc:
        parser.error(str(exc))
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1")
    return args


async def main(argv: list[str] | None = None, *, session_factory: Any = None) -> int:
    args = parse_args(argv)
    if session_factory is None:
        from infrastructure.config import Settings
        from infrastructure.db.session import session_factory_from_settings
        from infrastructure.logging import configure_logging

        configure_logging()
        session_factory = session_factory_from_settings(Settings())
    result = await reproject(
        session_factory,
        episode_id=args.episode,
        from_seq=args.from_seq,
        to_seq=args.to_seq,
        only_unprojected=not args.all,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    logger.info(
        "intervention reprojection finished emitted=%s marked=%s episodes=%s dry_run=%s",
        result.emitted,
        result.marked,
        result.episodes,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
