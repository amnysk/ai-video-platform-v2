"""``EpisodeReadPort`` の実装（ADR-0044 §2）。PostgreSQL と ArtifactStore（MinIO）だけを読む。

**何も書かない。** OpenSearch は読まない（検索基盤の遅れ・欠落で操作画面が嘘をつかない / INV-8）。
既存の ``EpisodeReadModel``・``read_pinned_agentic_template``・成果物 parser を再利用し、
ここに新しい意味づけは足さない。費用は ``None``（未見積り）を 0 にしない。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.agentic import QualityReview
from contracts.agentic_pipeline import (
    PipelineKind,
    agentic_episode_workflow_id,
)
from contracts.application import OperationError, OperationErrorCode
from contracts.application_operations import (
    ChangeImpactPreview,
    ChangeSetSummary,
    EpisodeList,
    EpisodeSummary,
    EpisodeWorkspace,
    FailureInfo,
    GetAgentDecisionsResult,
    GetQualityReviewResult,
    GetStoryboardResult,
    JobProgress,
    StoryboardView,
    WorkflowProgress,
)
from contracts.artifact_registry import parse_artifact
from contracts.artifacts import StoryboardArtifact
from contracts.intervention import AgentRunOutcome, AnnotationKind, ChangeSetStatus
from contracts.pipeline import pipeline_workflow_id
from contracts.states import ArtifactType, EpisodeStatus, JobStatus
from domain.episode.entities import Episode
from domain.job.entities import Job
from infrastructure.agents.pinned_plan import (
    has_unstarted_agentic_draft,
    read_pinned_agentic_template,
)
from infrastructure.db.models import ArtifactMetadataRow
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from infrastructure.intervention.read_model import (
    ChangeSetView,
    EpisodeReadModel,
    EpisodeView,
    format_usd,
)
from infrastructure.intervention.store import InterventionStore, manifest_slot
from infrastructure.storage.artifact_store import ArtifactStore

_FAILED_JOB_STATUSES = frozenset({JobStatus.RETRYABLE_FAILED, JobStatus.TERMINAL_FAILED})


def _not_found(episode_id: uuid.UUID) -> OperationError:
    return OperationError(OperationErrorCode.EPISODE_NOT_FOUND, f"episode {episode_id} not found")


def _workflow_id(episode_id: uuid.UUID, kind: PipelineKind) -> str:
    if kind is PipelineKind.AGENTIC:
        return agentic_episode_workflow_id(str(episode_id))
    return pipeline_workflow_id(str(episode_id))


def _summary(episode: Episode, kind: PipelineKind) -> EpisodeSummary:
    return EpisodeSummary(
        episode_id=uuid.UUID(episode.id),
        status=episode.status.value,
        topic=episode.topic,
        pipeline_kind=kind,
        created_at=episode.created_at,
        updated_at=episode.updated_at,
    )


def _change_set_summary(c: ChangeSetView) -> ChangeSetSummary:
    return ChangeSetSummary(
        change_set_id=c.change_set_id,
        kind=c.kind,
        status=c.status,
        base_revision_id=c.base_revision_id,
        estimated_cost_usd=c.estimated_cost_usd,
        cost_estimated=c.estimated_cost_usd is not None,
        proposed_by_command_id=c.proposed_by_command_id,
        actor_type=c.actor_type,
        created_at=c.created_at,
    )


class StoreBackedEpisodeReader:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store_factory: Callable[[], ArtifactStore],
    ) -> None:
        self._session_factory = session_factory
        self._store_factory = store_factory

    # ------------------------------------------------------------------ 内部

    async def _episode(self, episode_id: uuid.UUID) -> Episode:
        async with self._session_factory() as session:
            episode = await EpisodeRepository(session).get(episode_id)
        if episode is None:
            raise _not_found(episode_id)
        return episode

    async def _view(self, episode_id: uuid.UUID) -> EpisodeView:
        view = await EpisodeReadModel(self._session_factory, self._store_factory()).load(
            str(episode_id)
        )
        if view is None:
            raise _not_found(episode_id)
        return view

    # ------------------------------------------------------------------ EpisodeReadPort

    async def pipeline_kind(self, episode_id: uuid.UUID) -> PipelineKind:
        await self._episode(episode_id)
        pinned = await read_pinned_agentic_template(
            self._session_factory, self._store_factory, episode_id
        )
        return PipelineKind.LEGACY if pinned is None else PipelineKind.AGENTIC

    async def is_unstarted_agentic_draft(self, episode_id: uuid.UUID) -> bool:
        await self._episode(episode_id)
        return await has_unstarted_agentic_draft(self._session_factory, episode_id)

    async def list_episodes(
        self,
        *,
        limit: int,
        before: datetime | None,
        before_id: uuid.UUID | None = None,
        status: EpisodeStatus | None = None,
        q: str | None = None,
    ) -> EpisodeList:
        async with self._session_factory() as session:
            page = await EpisodeRepository(session).list_page(
                limit=limit + 1, before=before, before_id=before_id, status=status, q=q
            )
            agentic = await ArtifactMetadataRepository(session).episode_ids_with_current(
                ArtifactType.WORKFLOW_PLAN, [e.id for e in page]
            )
        shown = page[:limit]
        return EpisodeList(
            episodes=tuple(
                _summary(e, PipelineKind.AGENTIC if e.id in agentic else PipelineKind.LEGACY)
                for e in shown
            ),
            next_before=shown[-1].created_at if len(page) > limit else None,
            next_before_id=uuid.UUID(shown[-1].id) if len(page) > limit else None,
        )

    async def workspace(self, episode_id: uuid.UUID) -> EpisodeWorkspace:
        episode = await self._episode(episode_id)
        kind = await self.pipeline_kind(episode_id)
        view = await self._view(episode_id)
        return EpisodeWorkspace(
            episode=_summary(episode, kind),
            workflow_id=_workflow_id(episode_id, kind),
            control_revision=view.control.control_revision,
            pending_change_sets=tuple(
                _change_set_summary(c)
                for c in view.change_sets
                if c.status == ChangeSetStatus.PROPOSED.value
            ),
            read_model=view.model_dump(mode="json"),
        )

    async def storyboard(self, episode_id: uuid.UUID) -> GetStoryboardResult:
        await self._episode(episode_id)
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).find_current_by_type(
                episode_id, ArtifactType.STORYBOARD
            )
            if meta is None:
                return GetStoryboardResult(episode_id=episode_id, storyboard=None)
            control = await InterventionStore(session).get_control(str(episode_id))
            manifest = (
                None
                if control.active_manifest_id is None
                else await InterventionStore(session).get_manifest(control.active_manifest_id)
            )
            await session.rollback()
        document = await self._store_factory().get_json(meta.object_key)
        parsed = parse_artifact(ArtifactType.STORYBOARD, document)
        assert isinstance(parsed, StoryboardArtifact)
        in_manifest = (
            manifest is not None
            and manifest.members.get(manifest_slot(ArtifactType.STORYBOARD.value)) == meta.id
        )
        return GetStoryboardResult(
            episode_id=episode_id,
            storyboard=StoryboardView(
                episode_id=episode_id,
                revision_id=meta.id,
                manifest_id=manifest.manifest_id if in_manifest and manifest else None,
                schema_version=meta.schema_version,
                sha256=meta.sha256,
                scenes=parsed.scenes,
            ),
        )

    async def change_impact(self, episode_id: uuid.UUID, change_set_id: str) -> ChangeImpactPreview:
        await self._episode(episode_id)
        async with self._session_factory() as session:
            store = InterventionStore(session)
            try:
                change_set = await store.get_change_set(change_set_id)
            except ValueError:  # UUID でない id
                change_set = None
            if change_set is None or change_set.episode_id != str(episode_id):
                raise OperationError(
                    OperationErrorCode.CHANGE_SET_NOT_FOUND,
                    f"change set {change_set_id} not found in episode {episode_id}",
                )
            control = await store.get_control(str(episode_id))
            manifest = (
                None
                if control.active_manifest_id is None
                else await store.get_manifest(control.active_manifest_id)
            )
            base = await _artifact_row(session, change_set.base_revision_id)
            # 降ろされた版か（rollback で row が expire する前に読む）
            base_superseded = (
                base is not None
                and str(base.episode_id) == str(episode_id)
                and base.superseded_at is not None
            )
            await session.rollback()
        estimated = change_set.estimated_cost_usd
        blockers: list[str] = []
        if change_set.status is not ChangeSetStatus.PROPOSED:
            blockers.append(OperationErrorCode.INVALID_STATE.value)
        if estimated is None:
            blockers.append(OperationErrorCode.COST_NOT_ESTIMATED.value)
        in_manifest = manifest is not None and change_set.base_revision_id in set(
            manifest.members.values()
        )
        # 基準の revision が降ろされ、採用中の manifest にも無ければ、この案は古い版への変更
        if base_superseded and not in_manifest:
            blockers.append(OperationErrorCode.STALE_TARGET_REVISION.value)
        return ChangeImpactPreview(
            episode_id=episode_id,
            change_set_id=change_set.change_set_id,
            kind=change_set.kind.value,
            status=change_set.status.value,
            base_revision_id=change_set.base_revision_id,
            base_manifest_id=change_set.base_manifest_id,
            proposal=change_set.proposal,
            impact=change_set.impact,
            estimated_cost_usd=None if estimated is None else format_usd(estimated),
            cost_estimated=estimated is not None,
            control_revision=control.control_revision,
            blockers=tuple(blockers),
            decided_by_command_id=change_set.decided_by_command_id,
        )

    async def agent_decisions(self, episode_id: uuid.UUID) -> GetAgentDecisionsResult:
        view = await self._view(episode_id)
        return GetAgentDecisionsResult(
            episode_id=episode_id,
            decisions=tuple(d.model_dump(mode="json") for d in view.decisions),
            agent_runs=tuple(r.model_dump(mode="json") for r in view.agent_runs),
        )

    async def quality_review(self, episode_id: uuid.UUID) -> GetQualityReviewResult:
        await self._episode(episode_id)
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).find_current_by_type(
                episode_id, ArtifactType.QUALITY_REVIEW
            )
        if meta is None:
            return GetQualityReviewResult(
                episode_id=episode_id,
                status="not_reviewed",
                review=None,
                artifact_id=None,
                findings=(),
            )
        parsed = parse_artifact(
            ArtifactType.QUALITY_REVIEW, await self._store_factory().get_json(meta.object_key)
        )
        assert isinstance(parsed, QualityReview)
        view = await self._view(episode_id)
        return GetQualityReviewResult(
            episode_id=episode_id,
            status="reviewed",
            review=parsed,
            artifact_id=meta.id,
            findings=tuple(
                a.model_dump(mode="json")
                for a in view.annotations
                if a.kind == AnnotationKind.REVIEW_FINDING.value
            ),
        )

    async def progress(self, episode_id: uuid.UUID) -> WorkflowProgress:
        episode = await self._episode(episode_id)
        kind = await self.pipeline_kind(episode_id)
        async with self._session_factory() as session:
            jobs = await JobRepository(session).list_for_episode(episode_id)
        view = await self._view(episode_id)
        return WorkflowProgress(
            episode_id=episode_id,
            episode_status=episode.status.value,
            pipeline_kind=kind,
            workflow_id=_workflow_id(episode_id, kind),
            jobs=tuple(
                JobProgress(
                    job_id=j.id,
                    type=j.type.value,
                    status=j.status.value,
                    attempts=j.attempts,
                    max_attempts=j.max_attempts,
                    scene_id=j.scene_id,
                    updated_at=j.updated_at,
                )
                for j in jobs
            ),
            agent_runs=tuple(r.model_dump(mode="json") for r in view.agent_runs),
            failure=_latest_failure(jobs, view),
        )


async def _artifact_row(session: AsyncSession, revision_id: str) -> ArtifactMetadataRow | None:
    try:
        return await session.get(ArtifactMetadataRow, uuid.UUID(revision_id))
    except ValueError:
        return None


def _latest_failure(jobs: list[Job], view: EpisodeView) -> FailureInfo | None:
    """直近の失敗。失敗状態の job → 直近の agent run が成功でないもの → Episode の blocked_reason。

    過去に失敗して再試行で成功した job・run は「直近の失敗」ではない（状態で絞る）。
    """
    failed = [j for j in jobs if j.status in _FAILED_JOB_STATUSES and j.failure_class is not None]
    if failed:
        job = max(failed, key=lambda j: (j.updated_at.isoformat(), j.id))
        assert job.failure_class is not None
        return FailureInfo(
            code=job.failure_class.value,
            message=None,
            source="job",
            source_id=job.id,
            at=job.updated_at,
        )
    if view.agent_runs:
        run = max(view.agent_runs, key=lambda r: r.created_at)
        if run.outcome != AgentRunOutcome.SUCCEEDED.value:
            return FailureInfo(
                code=run.outcome,
                message=None,
                source="agent_run",
                source_id=run.agent_run_id,
                at=run.created_at,
            )
    if view.blocked_reason:
        return FailureInfo(
            code="blocked",
            message=view.blocked_reason,
            source="episode",
            source_id=view.episode_id,
            at=None,
        )
    return None
