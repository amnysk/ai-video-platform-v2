"""Episode 単体の読み取り専用インシデント診断（この incident の再開判断を助けるためだけの道具）。

**判定ロジックは一切ここに無い**（AGENTS.md §8）。既存の決定関数をそのまま呼び、結果を集めて
整形するだけ:

- ``domain.pipeline.resume_plan.build_resume_plan``（ADR-0032）── 次にどの工程から再開できるか
- ``infrastructure.artifact.verify.verify_artifact``（ADR-0033）── 個々の Artifact が実体まで
  再利用可能か

読み取り専用の保証: ``session.commit()`` を一度も呼ばない。provider 呼び出し・予約 INSERT・
workflow start は行わない（このモジュールが import する repository メソッドは SELECT のみ）。

生成設定版（``generation_profile_id``）の「現在有効な値」は呼び出し側が ``profile_for`` として
渡す（ADR-0033 と同じ設計: fal の固定定数をここに埋め込まない。fal adapter を import しないので
``tests/architecture/test_no_live_calls.py::test_only_sanctioned_modules_import_fal_adapters`` の
allowlist に触れない）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.states import ArtifactType, EpisodeStatus, ProviderCall
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.verification import ArtifactVerdict
from domain.pipeline.resume_plan import ReservationSummary, ResumePlan, build_resume_plan
from infrastructure.artifact.verify import verify_artifact
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    ProviderReservationRepository,
)
from infrastructure.storage.artifact_store import ArtifactStore

#: 型ごとに「現在有効な生成設定版」を返す。この型のチェックが無意味/未対応なら None を返すこと
#: （``verify_artifact`` は None なら未評価のまま安全側 REUSABLE 判定には倒さない設計。
#: ADR-0033 §2-3）。
ProfileResolver = Callable[[ArtifactType], "str | None"]

__all__ = [
    "ArtifactVerificationRow",
    "EpisodeDiagnosticReport",
    "ReservationRow",
    "diagnose_episode",
    "format_report",
    "verify_episode_artifacts",
]


@dataclass(frozen=True, slots=True)
class ArtifactVerificationRow:
    artifact_type: ArtifactType
    scene_id: str | None
    artifact_id: str
    object_key: str
    verdict: ArtifactVerdict
    detail: str


@dataclass(frozen=True, slots=True)
class ReservationRow:
    id: str
    provider: ProviderCall
    scene_id: str | None
    round: int
    status: str
    reconciled_by: str | None
    has_raw_output: bool
    outcome_artifact_id: str | None
    estimated_cost_usd: str | None


@dataclass(frozen=True, slots=True)
class EpisodeDiagnosticReport:
    episode_id: str
    status: EpisodeStatus
    blocked_reason: str | None
    workflow_id: str | None
    resume_plan: ResumePlan
    artifact_verifications: tuple[ArtifactVerificationRow, ...]
    reservations: tuple[ReservationRow, ...]
    upload_receipt_present: bool


async def verify_episode_artifacts(
    store: ArtifactStore,
    artifacts: Sequence[ArtifactMetadata],
    *,
    profile_for: ProfileResolver,
) -> tuple[ArtifactVerificationRow, ...]:
    """既存の ``verify_artifact``（ADR-0033）を各 Artifact に対して呼ぶだけ。整形のみ。"""
    rows: list[ArtifactVerificationRow] = []
    for artifact in artifacts:
        result = await verify_artifact(
            store,
            artifact,
            current_generation_profile_id=profile_for(artifact.artifact_type),
        )
        rows.append(
            ArtifactVerificationRow(
                artifact_type=artifact.artifact_type,
                scene_id=artifact.scene_id,
                artifact_id=artifact.id,
                object_key=artifact.object_key,
                verdict=result.verdict,
                detail=result.detail,
            )
        )
    return tuple(rows)


def _to_reservation_row(row: object) -> ReservationRow:
    # infrastructure.db.repositories.ProviderReservation (duck-typed to avoid a second import
    # cycle; the attributes below are that dataclass's public fields).
    return ReservationRow(
        id=row.id,  # type: ignore[attr-defined]
        provider=row.provider,  # type: ignore[attr-defined]
        scene_id=row.scene_id,  # type: ignore[attr-defined]
        round=row.round,  # type: ignore[attr-defined]
        status=row.status.value if hasattr(row.status, "value") else str(row.status),  # type: ignore[attr-defined]
        reconciled_by=row.reconciled_by,  # type: ignore[attr-defined]
        has_raw_output=row.raw_output_key is not None,  # type: ignore[attr-defined]
        outcome_artifact_id=row.outcome_artifact_id,  # type: ignore[attr-defined]
        estimated_cost_usd=(
            str(row.estimated_cost_usd) if row.estimated_cost_usd is not None else None  # type: ignore[attr-defined]
        ),
    )


async def diagnose_episode(
    episode_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    *,
    profile_for: ProfileResolver,
) -> EpisodeDiagnosticReport | None:
    """1 Episode の読み取り専用診断。``None`` は Episode が無い場合。

    DB 読み取りは ``EpisodeRepository`` / ``ArtifactMetadataRepository`` /
    ``ProviderReservationRepository`` の既存 SELECT メソッドだけを使う（新しい SQL を書かない）。
    ``build_resume_plan`` の呼び方は ``apps/api/routers/episodes.py::_load_resume_plan`` と同じ
    形（未照合予約は全 provider を対象に読む。判定自体は二重化しない、同じ関数を呼ぶだけ）。
    """
    async with session_factory() as session:
        episode_repo = EpisodeRepository(session)
        episode = await episode_repo.get(episode_id)
        if episode is None:
            return None
        owner_workflow_id = await episode_repo.get_workflow_id(episode.id)
        # ``Episode``（ドメイン実体）は blocked_reason を持たない（状態遷移の権威には不要な
        # 診断用テキストのため）。既存の read-only ビュー（watchdog が使うのと同じメソッド、
        # ADR-0031）を再利用して読む。新しい SQL は書かない。
        snapshots = await episode_repo.list_progress_snapshots([episode.status])
        blocked_reason = next(
            (s.blocked_reason for s in snapshots if str(s.id) == episode.id), None
        )

        reservation_repo = ProviderReservationRepository(session)
        all_reservations = []
        for provider in ProviderCall:
            all_reservations.extend(
                await reservation_repo.list_for_episode_provider(episode.id, provider)
            )

        artifact_repo = ArtifactMetadataRepository(session)
        artifacts = await artifact_repo.list_for_episode(episode.id)

    unreconciled_summaries = [
        ReservationSummary(
            id=r.id, provider=r.provider, status=r.status, raw_output_key=r.raw_output_key
        )
        for r in all_reservations
    ]
    plan = build_resume_plan(
        episode_id=str(episode.id),
        status=episode.status,
        owner_workflow_id=owner_workflow_id,
        unreconciled_reservations=unreconciled_summaries,
    )

    verifications = await verify_episode_artifacts(store, artifacts, profile_for=profile_for)
    upload_receipt_present = any(a.artifact_type is ArtifactType.UPLOAD_RECEIPT for a in artifacts)

    return EpisodeDiagnosticReport(
        episode_id=str(episode.id),
        status=episode.status,
        blocked_reason=blocked_reason,
        workflow_id=owner_workflow_id,
        resume_plan=plan,
        artifact_verifications=verifications,
        reservations=tuple(_to_reservation_row(r) for r in all_reservations),
        upload_receipt_present=upload_receipt_present,
    )


def format_report(report: EpisodeDiagnosticReport) -> str:
    """人が読める形式に整形するだけ（判定は一切しない）。"""
    lines: list[str] = []
    lines.append(f"Episode {report.episode_id}")
    lines.append(f"  status: {report.status.value}")
    lines.append(f"  blocked_reason: {report.blocked_reason}")
    lines.append(f"  workflow_id: {report.workflow_id}")
    lines.append("")
    lines.append("Resume plan (domain.pipeline.resume_plan.build_resume_plan):")
    lines.append(f"  resumable: {report.resume_plan.resumable}")
    lines.append(f"  target_stage: {report.resume_plan.target_stage}")
    lines.append(f"  stages_to_run: {list(report.resume_plan.stages_to_run)}")
    lines.append(f"  possible_new_charges: {list(report.resume_plan.possible_new_charges)}")
    lines.append(f"  unresolved_blockers: {list(report.resume_plan.unresolved_blockers)}")
    lines.append(
        f"  unreconciled_reservations: "
        f"{[r.id for r in report.resume_plan.unreconciled_reservations]}"
    )
    lines.append("")
    lines.append("Artifact verification (infrastructure.artifact.verify.verify_artifact):")
    by_type: dict[str, list[ArtifactVerificationRow]] = {}
    for row in report.artifact_verifications:
        by_type.setdefault(row.artifact_type.value, []).append(row)
    for artifact_type in sorted(by_type):
        for row in sorted(by_type[artifact_type], key=lambda r: r.scene_id or ""):
            scene = row.scene_id or "-"
            lines.append(
                f"  [{row.verdict.value:>16}] {artifact_type:14} scene={scene:4} {row.object_key}"
            )
    lines.append("")
    lines.append("Provider reservations:")
    for row in report.reservations:
        scene = row.scene_id or "-"
        lines.append(
            f"  provider={row.provider.value:17} scene={scene:4} round={row.round} "
            f"status={row.status:8} reconciled_by={row.reconciled_by} "
            f"has_raw_output={row.has_raw_output} outcome_artifact_id={row.outcome_artifact_id} "
            f"estimated_cost_usd={row.estimated_cost_usd}"
        )
    lines.append("")
    lines.append(f"upload_receipt_present: {report.upload_receipt_present}")
    return "\n".join(lines)
