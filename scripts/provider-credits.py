#!/usr/bin/env python
"""fal の残高不足の保留の確認・breaker の承認つき遷移・Episode の再開（ADR-0057）。出力は JSON。

    python scripts/provider-credits.py status
    python scripts/provider-credits.py half-open --owner-approval TOKEN --evidence TEXT \\
        --actor NAME [--apply]
    python scripts/provider-credits.py close --owner-approval TOKEN --evidence TEXT \\
        --actor NAME [--apply]
    python scripts/provider-credits.py resume (--episode-id ID ... | --oldest N) \\
        --owner-approval TOKEN --evidence TEXT --actor NAME \\
        --estimated-additional-usd X [--api-url URL --apply]

- ``status``: 読み取りだけ。breaker・保留 Episode・未照合予約・当月の見積支出・予算上限。
- ``half-open`` / ``close``: 所有者承認 token（``OWNER-<yyyymmdd>-<id>``。認証ではない）と、
  入金を確認した根拠（``--evidence``）が必須。**この CLI は fal へ接続しない**（残高を読む
  公式の無課金・読み取り専用 endpoint が無いため、確認は所有者の attestation）。
- ``resume``: 明示した Episode か、保留の古い順 N 件。同時に再開できるのは
  ``RESUME_MAX_CONCURRENCY``。未照合の予約がある・当月の予算を超える・上限が未設定・breaker が open
  なら拒否。通れば**既存の Episode 再開 API** を呼ぶ（新しい workflow は作らない）。
  成功済みシーンは再利用ゲートが再送せず再利用する。
- 既定は dry-run。``--apply`` でだけ書く。

終了コード: 0 = 正常 / 2 = 拒否（理由は JSON の ``error``）。承認 token は出力しない。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from infrastructure.db.provider_health_repositories import PostgresProviderHealth  # noqa: E402
from infrastructure.production import provider_credits as credits  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="読み取り専用")
    for name in ("half-open", "close", "resume"):
        sp = sub.add_parser(name)
        sp.add_argument("--owner-approval", default="")
        sp.add_argument("--evidence", default="")
        sp.add_argument("--actor", default="")
        sp.add_argument("--apply", action="store_true")
        if name == "resume":
            sp.add_argument("--episode-id", action="append", default=[])
            sp.add_argument("--oldest", type=int)
            sp.add_argument("--estimated-additional-usd", default="")
            sp.add_argument("--api-url", default="")
    return p


async def run(
    argv: list[str],
    *,
    session_factory: async_sessionmaker[AsyncSession],
    budget_cap: Decimal | None,
    resume_client: credits.ResumeClient | None = None,
) -> tuple[int, dict[str, Any]]:
    """CLI の本体（試験から直接呼ぶ）。``(終了コード, JSON 化できる結果)`` を返す。"""
    args = _parser().parse_args(argv)
    health = PostgresProviderHealth(session_factory)
    try:
        if args.command == "status":
            return EXIT_OK, await credits.status(session_factory, health, budget_cap=budget_cap)
        approval = credits.validate_approval(
            owner_approval=args.owner_approval, evidence=args.evidence, actor=args.actor
        )
        if args.command in ("half-open", "close"):
            return EXIT_OK, await credits.set_breaker(
                health, to=args.command, approval=approval, apply=args.apply
            )
        try:
            additional = Decimal(args.estimated_additional_usd)
        except InvalidOperation:
            raise credits.CreditsRefused(
                "--estimated-additional-usd is required (a number, USD)"
            ) from None
        client = resume_client
        if client is None and args.api_url:
            client = credits.ApiResumeClient(args.api_url)
        return EXIT_OK, await credits.resume_episodes(
            session_factory,
            health,
            client,
            episode_ids=args.episode_id,
            oldest=args.oldest,
            approval=approval,
            estimated_additional_usd=additional,
            budget_cap=budget_cap,
            apply=args.apply,
        )
    except (credits.CreditsRefused, credits.ResumeRejected) as exc:
        return EXIT_REFUSED, {"command": args.command, "error": str(exc)}


async def main(argv: list[str]) -> int:
    from infrastructure.config import Settings

    settings = Settings()
    engine = create_async_engine(settings.database_url)
    try:
        code, result = await run(
            argv,
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            budget_cap=settings.fal_monthly_budget_usd,
        )
    finally:
        await engine.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
