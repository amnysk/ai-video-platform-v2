#!/usr/bin/env python
"""読み取り専用の release preflight（ADR-0050 / INV-65）。

    python scripts/release-preflight.py --target-sha <40hex> --backup-manifest PATH [--json]
    python scripts/release-preflight.py --verify-post-deploy --target-sha <40hex> \\
        --pre-report preflight.json

総合: READY（全必須が PASS）/ BLOCKED / UNKNOWN。終了コード READY=0 / BLOCKED=2 / UNKNOWN=3。
UNKNOWN は READY として扱わない。何も変更しない（deploy・pause・再起動は別の手順）。
ロジックは ``domain/release_preflight.py``、collector は ``infrastructure/release/collectors.py``。
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from domain.release_preflight import (  # noqa: E402
    DEFAULT_BACKUP_MAX_AGE_HOURS,
    DEFAULT_EVIDENCE_MAX_AGE_HOURS,
    EXIT_CODES,
    MAX_EVIDENCE_MAX_AGE_HOURS,
    Collectors,
    Overall,
    Request,
    exit_code,
    render_text,
    report_json,
    run_preflight,
)

REPO = pathlib.Path(__file__).resolve().parents[1]
#: 使い方の誤り・想定外の失敗。READY(0) ではなく UNKNOWN と同じ 3（BLOCKED=2 とは区別する）
EXIT_USAGE = EXIT_CODES[Overall.UNKNOWN]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--target-sha", required=True, help="deploy する commit（40桁 hex）")
    p.add_argument(
        "--allow-non-ff",
        action="store_true",
        help="本番 revision から fast-forward でなくても許す（rollback 等）",
    )
    p.add_argument("--backup-manifest", help="バックアップ manifest(JSON)。無ければ UNKNOWN")
    p.add_argument("--backup-max-age-hours", type=float, default=DEFAULT_BACKUP_MAX_AGE_HOURS)
    p.add_argument("--replay-evidence", help="既定 release/evidence/<sha>-replay.json")
    p.add_argument("--approval-file", help="既定 release/approvals/<sha>.json")
    p.add_argument(
        "--approved-report",
        help="owner が承認した preflight の --json 出力。承認ファイルの report_sha256 と照合する",
    )
    p.add_argument(
        "--evidence-max-age-hours",
        type=float,
        default=DEFAULT_EVIDENCE_MAX_AGE_HOURS,
        help=f"承認・replay 証跡の最大の古さ（既定 72、上限 {MAX_EVIDENCE_MAX_AGE_HOURS:g}）",
    )
    p.add_argument(
        "--rollback",
        action="store_true",
        help="前の SHA へ戻す preflight / 事後検証（DB が先でも expand-only なら判定する）",
    )
    p.add_argument("--verify-post-deploy", action="store_true")
    p.add_argument(
        "--pre-report", help="--verify-post-deploy: deploy 前の preflight の --json 出力"
    )
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None, collectors: Collectors | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        # argparse の既定の終了コード 2 は BLOCKED と同じ。使い方の誤りは UNKNOWN(3)（--help は 0）
        return 0 if exc.code in (0, None) else EXIT_USAGE
    try:
        return _run(args, collectors)
    except Exception as exc:  # 想定外でも READY(0) や素のトレースバックにしない。詳細は出さない
        print(f"release preflight failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_USAGE


def _run(args: argparse.Namespace, collectors: Collectors | None) -> int:
    if collectors is None:
        from infrastructure.release.collectors import RealCollectors

        collectors = RealCollectors(REPO)
    request = Request(
        target_sha=args.target_sha,
        allow_non_ff=args.allow_non_ff,
        backup_manifest=args.backup_manifest,
        backup_max_age_hours=args.backup_max_age_hours,
        replay_evidence=args.replay_evidence,
        approval_file=args.approval_file,
        verify_post_deploy=args.verify_post_deploy,
        pre_report=args.pre_report,
        approved_report=args.approved_report,
        evidence_max_age_hours=args.evidence_max_age_hours,
        rollback=args.rollback,
    )
    report = run_preflight(request, collectors, datetime.now(UTC))
    print(report_json(report) if args.json else render_text(report))
    return exit_code(report.overall)


if __name__ == "__main__":
    raise SystemExit(main())
