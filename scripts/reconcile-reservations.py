#!/usr/bin/env python
"""未照合の provider 予約の検出・照合・人手確定（ADR-0053 / INV-67）。出力は JSON。

    python scripts/reconcile-reservations.py list
    python scripts/reconcile-reservations.py reconcile [--apply] --observations-file PATH
    python scripts/reconcile-reservations.py resolve --reservation-id ID \\
        --outcome success|failure|cancelled \\
        --evidence TEXT --owner-approval TOKEN --actor NAME [--apply]

- ``list``: 読み取り専用（DB 側も READ ONLY transaction）。未照合の予約と経過時間・判定。
- ``reconcile``: **既定は dry-run**。注入された provider status 関数の結果から判定を計画する。
  ``--apply`` で判定行を**追記**する（台帳の status は変えない）。この CLI は provider へ
  接続しない: status 関数は ``--observations-file``（運用者が取得した観測 JSON）か、
  プログラムからの注入だけ。生の fal 呼び出しは owner 承認が要る別手順。
- ``resolve``: 人手の確定。``--evidence`` と ``--owner-approval`` が必須。既定は dry-run、
  ``--apply`` で監査行を足して台帳を動かす。同じ結論の再実行は何もしない。

終了コード: 0 = 正常 / 2 = 入力不正・拒否 / 3 = provider status 源が無い。
再送・resubmit・DB の行の更新削除・YouTube は行わない。secret（承認 token・API キー）は
出力・ログに出さない（承認 token は指紋だけ）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from collections.abc import Awaitable, Callable
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from domain.provider.reconciliation import (  # noqa: E402
    ManualResolutionError,
    ProviderObservation,
    UnreconciledView,
    advance_verdict,
    classify_observation,
    validate_manual_resolution,
)
from infrastructure.db.repositories import ProviderReconciliationRepository  # noqa: E402

EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_NO_STATUS_SOURCE = 3

#: provider へ**読み取り**だけで問い合わせる関数（注入される。この CLI は実装を持たない）。
StatusFn = Callable[[UnreconciledView], Awaitable[ProviderObservation]]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="未照合の予約を一覧する（読み取り専用）")
    r = sub.add_parser("reconcile", help="provider の観測から判定を計画する（既定 dry-run）")
    r.add_argument("--apply", action="store_true", help="判定行を追記する")
    r.add_argument("--observations-file", help="{reservation_id: {queue_status, result_outcome}}")
    m = sub.add_parser("resolve", help="人手で確定する（evidence と owner 承認が必須）")
    m.add_argument("--reservation-id", required=True)
    m.add_argument("--outcome", required=True)
    m.add_argument("--evidence", default="")
    m.add_argument("--owner-approval", default="")
    m.add_argument("--actor", default="")
    m.add_argument("--apply", action="store_true")
    return p


def _file_status_fn(path: str) -> StatusFn:
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))

    async def fn(view: UnreconciledView) -> ProviderObservation:
        entry = data.get(view.reservation_id)
        if not isinstance(entry, dict):
            return ProviderObservation(error="no observation supplied")
        outcome = entry.get("result_outcome")
        return ProviderObservation(
            queue_status=entry.get("queue_status"),
            result_outcome=outcome if outcome in ("ok", "failed") else None,
            error=entry.get("error"),
        )

    return fn


async def _read_only(session: AsyncSession) -> None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SET TRANSACTION READ ONLY"))


async def run(
    argv: list[str],
    *,
    session_factory: async_sessionmaker[AsyncSession],
    status_fn: StatusFn | None = None,
) -> tuple[int, dict[str, Any]]:
    """CLI の本体（試験から直接呼ぶ）。``(終了コード, JSON 化できる結果)`` を返す。"""
    args = _parser().parse_args(argv)
    if args.command == "list":
        async with session_factory() as session:
            await _read_only(session)
            summary = await ProviderReconciliationRepository(session).unreconciled_summary()
            await session.rollback()
        return EXIT_OK, {"command": "list", **summary}

    if args.command == "reconcile":
        if status_fn is None and args.observations_file:
            status_fn = _file_status_fn(args.observations_file)
        if status_fn is None:
            return EXIT_NO_STATUS_SOURCE, {
                "command": "reconcile",
                "error": "no provider status source; supply --observations-file (a live provider "
                "status call needs owner approval)",
            }
        return await _reconcile(session_factory, status_fn, apply=args.apply)

    try:
        resolution = validate_manual_resolution(
            outcome=args.outcome,
            evidence=args.evidence,
            owner_approval=args.owner_approval,
            actor=args.actor,
        )
    except ManualResolutionError as exc:
        return EXIT_REJECTED, {"command": "resolve", "error": str(exc)}
    async with session_factory() as session:
        repo = ProviderReconciliationRepository(session)
        try:
            if not args.apply:
                await _read_only(session)
                current = await repo.latest_verdict(args.reservation_id)
                step = advance_verdict(current, resolution.verdict)
                result = {
                    "command": "resolve",
                    "dry_run": True,
                    "reservation_id": args.reservation_id,
                    "would_write": step.changed,
                    "verdict": resolution.verdict.value,
                    "conflict": step.conflict,
                }
                await session.rollback()
                return (EXIT_REJECTED if step.conflict else EXIT_OK), result
            wrote = await repo.resolve_manual(args.reservation_id, resolution)
            await session.commit()
        except ManualResolutionError as exc:
            await session.rollback()
            return EXIT_REJECTED, {"command": "resolve", "error": str(exc)}
    return EXIT_OK, {
        "command": "resolve",
        "dry_run": False,
        "reservation_id": args.reservation_id,
        "wrote": wrote,
        "verdict": resolution.verdict.value,
        "actor": resolution.actor,
        "owner_approval_fingerprint": resolution.owner_approval_fingerprint,
    }


async def _reconcile(
    session_factory: async_sessionmaker[AsyncSession], status_fn: StatusFn, *, apply: bool
) -> tuple[int, dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    async with session_factory() as session:
        if not apply:
            await _read_only(session)
        repo = ProviderReconciliationRepository(session)
        for view in await repo.list_unreconciled():
            try:
                observation = await status_fn(view)
            except Exception as exc:  # 観測できないのは UNKNOWN。失敗にしない
                observation = ProviderObservation(error=type(exc).__name__)
            observed = classify_observation(observation)
            step = advance_verdict(await repo.latest_verdict(view.reservation_id), observed)
            plans.append(
                {
                    "reservation_id": view.reservation_id,
                    "episode_id": view.episode_id,
                    "provider_request_id": view.provider_request_id,
                    "before": view.verdict.value,
                    "observed": observed.value,
                    "after": step.verdict.value,
                    "would_write": step.changed,
                    "conflict": step.conflict,
                }
            )
            if apply:
                await repo.record_observation(view.reservation_id, observation)
        if apply:
            await session.commit()
        else:
            await session.rollback()
    return EXIT_OK, {"command": "reconcile", "dry_run": not apply, "plans": plans}


async def main(argv: list[str]) -> int:
    from infrastructure.config import Settings

    engine = create_async_engine(Settings().database_url)
    try:
        code, result = await run(argv, session_factory=async_sessionmaker(engine))
    finally:
        await engine.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
