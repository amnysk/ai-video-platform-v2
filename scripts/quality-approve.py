#!/usr/bin/env python
"""品質ゲートの REVIEW を所有者が解放する host CLI（ADR-0081 §9a / INV-129）。出力は JSON。

    python scripts/quality-approve.py <episode_id> \\
        --owner-approval OWNER-<yyyymmdd>-<id> [--execute]

**これは所有者本人の行為**。API 認証が無効（``AVP_API_AUTH_MODE=disabled``。ADR-0080 追補）の
本番では ``get_upload_approver`` が常に ``None`` で、API からは REVIEW を解放できない。
この CLI が唯一の経路である。コンテナ内のエージェントが暗黙に使う道ではない: host で所有者が、
``--owner-approval``（所有者本人の参照）と ``--execute`` を明示して実行する。

- 既定は dry-run: ``evaluate(manual=True)`` の結果（outcome と理由）を出す。承認行も
  workflow 起動も無い（検査記録・報告は gate の通常動作として追記される）。
- ``--owner-approval``: ``OWNER-<yyyymmdd>-<id>`` だけ。``AGENT-…``（委任）・種別なし・形違いは
  拒否（``domain.approval.parse_approval_reference`` を使う。認証ではなく形式検査）。
- ``--execute``: REVIEW を解放できるときだけ、API の手動投稿 ``start_upload`` をそのまま呼ぶ
  （承認の記録 → 同じ workflow id での起動 / 二重投稿しない）。BLOCK・記録欠落・例外・flag OFF・
  投稿受領済みは何も書かず終了コード 2。承認が書けなければ workflow は起動しない。
  公開範囲は private のまま（契約）。

終了コード: 0 = 正常 / 2 = 拒否（理由は JSON の ``error``）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from apps.api.routers.episodes import start_upload  # noqa: E402
from contracts.approval import ApproverKind  # noqa: E402
from contracts.states import ArtifactType  # noqa: E402
from domain.approval import ApprovalError, parse_approval_reference  # noqa: E402
from domain.quality.approval import UploadApprover  # noqa: E402
from infrastructure.db.repositories import ArtifactMetadataRepository  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 2
#: approver_id に残す接頭辞（API 経由の ``api-owner-token`` と区別する）
APPROVER_ID_PREFIX = "host-cli-"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("episode_id")
    p.add_argument("--owner-approval", default="")
    p.add_argument("--execute", action="store_true")
    return p


def _refused(error: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    return EXIT_REFUSED, {"error": error, **extra}


async def run(
    argv: list[str], *, session_factory: Any, service: Any, starter: Any, settings: Any
) -> tuple[int, dict[str, Any]]:
    """CLI の本体（試験から直接呼ぶ）。``(終了コード, JSON 化できる結果)``。"""
    args = _parser().parse_args(argv)
    try:
        reference = parse_approval_reference(args.owner_approval)
    except ApprovalError as exc:
        return _refused(str(exc))
    if reference.kind is not ApproverKind.OWNER:
        return _refused(
            "only the owner's own approval (OWNER-<yyyymmdd>-<id>) can release a REVIEW"
        )
    try:
        episode_id = str(uuid.UUID(args.episode_id))
    except ValueError:
        return _refused("episode_id must be a UUID")
    if service is None:
        return _refused("the quality gate is not enforced (AVP_QUALITY_GATE_ENFORCE is off)")
    approver = UploadApprover(ApproverKind.OWNER, APPROVER_ID_PREFIX + reference.reference)

    try:
        async with session_factory() as session:
            receipt = await ArtifactMetadataRepository(session).find_current_hashes(
                episode_id, ArtifactType.UPLOAD_RECEIPT
            )
    except Exception as exc:  # noqa: BLE001 - 確かめられないものは進めない
        return _refused(f"upload receipt could not be checked: {type(exc).__name__}")
    if receipt is not None:
        return _refused("an upload receipt already exists; the video is never uploaded twice")

    try:
        outcome = await service.evaluate(episode_id, manual=True)
    except Exception as exc:  # noqa: BLE001 - fail-closed
        return _refused(f"quality evaluation failed: {type(exc).__name__}")
    assessment = outcome.assessment
    summary = {
        "episode_id": episode_id,
        "allowed": assessment.decision.allowed,
        "reason_code": assessment.decision.reason_code.value,
        "verdict": assessment.verdict.value if assessment.verdict else None,
        "review_approved": assessment.review_approved,
        "final_video_sha256": outcome.final_video_sha256,
        "executed": False,
    }
    if not args.execute:
        return EXIT_OK, summary
    if not (assessment.decision.allowed and assessment.review_approved):
        return _refused(
            "not a REVIEW that can be released (BLOCK, missing records, or already PASS/WARN: "
            "use the API for those)",
            **summary,
        )
    try:
        response = await start_upload(
            uuid.UUID(episode_id),
            session_factory,
            starter,
            settings,
            (),
            lambda: datetime.now(UTC),
            None,
            service,
            approver,
        )
    except HTTPException as exc:
        return _refused(f"HTTP {exc.status_code}: {exc.detail}", **summary)
    except Exception as exc:  # noqa: BLE001
        return _refused(f"release failed: {type(exc).__name__}", **summary)
    return EXIT_OK, {**summary, "executed": True, "workflow_id": response.workflow_id}


async def main(argv: list[str]) -> int:
    from apps.api.workflow_starter import TemporalWorkflowStarter
    from infrastructure.config import Settings
    from infrastructure.db.session import session_factory_from_settings
    from infrastructure.quality.wiring import build_quality_service

    settings = Settings()
    session_factory = session_factory_from_settings(settings)
    service = build_quality_service(settings, session_factory)
    # dry-run は Temporal に繋がない（起動しないので不要）
    starter = await TemporalWorkflowStarter.connect(settings) if "--execute" in argv else None
    code, result = await run(
        argv,
        session_factory=session_factory,
        service=service,
        starter=starter,
        settings=settings,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
