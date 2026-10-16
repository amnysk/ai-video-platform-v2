"""agentic Episode の draft の DB 実装（``application.ports.EpisodeDraftPort`` を構造的に満たす）。

Episode id は ``command_id`` から決定的に決める（``contracts.application.episode_draft_id``）ので、
同じ ``command_id`` の再送は同じ行に当たり、並行しても主キーが1つに絞る。新しい表は作らない。
日次の枠（``daily_episode_slots``）・TopicPlan には触れない。
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.agentic import UserBrief
from contracts.application import (
    DraftRecord,
    OperationError,
    OperationErrorCode,
    episode_draft_id,
)
from contracts.intervention import InterventionMode
from contracts.states import ArtifactType
from infrastructure.agents.episode_artifacts import read_current_artifact, write_episode_artifact
from infrastructure.db.repositories import EpisodeRepository
from infrastructure.intervention.store import ControlChange, InterventionStore
from infrastructure.storage.artifact_store import ArtifactStore


class DbEpisodeDraftPort:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        bucket: str,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._bucket = bucket

    async def _stored_brief(self, session: AsyncSession, episode_id: uuid.UUID) -> UserBrief | None:
        current = await read_current_artifact(
            session, self._store, episode_id, ArtifactType.USER_BRIEF
        )
        if current is None:
            return None
        assert isinstance(current[1], UserBrief)
        return current[1]

    async def get_brief(self, episode_id: uuid.UUID) -> UserBrief | None:
        async with self._session_factory() as session:
            if await EpisodeRepository(session).get(episode_id) is None:
                return None
            return await self._stored_brief(session, episode_id)

    async def _replay(
        self, session: AsyncSession, episode_id: uuid.UUID, brief: UserBrief, command_id: str
    ) -> DraftRecord:
        stored = await self._stored_brief(session, episode_id)
        if stored != brief:
            raise OperationError(
                OperationErrorCode.COMMAND_ID_REUSED,
                f"command_id {command_id!r} was already used with a different brief",
            )
        return DraftRecord(episode_id=episode_id, created=False)

    async def create_draft(self, *, command_id: str, brief: UserBrief) -> DraftRecord:
        episode_id = episode_draft_id(command_id)
        async with self._session_factory() as session:
            if await EpisodeRepository(session).get(episode_id) is not None:
                return await self._replay(session, episode_id, brief, command_id)
            try:
                await EpisodeRepository(session).create(topic=brief.theme, episode_id=episode_id)
                await write_episode_artifact(
                    session, self._store, bucket=self._bucket, episode_id=episode_id, model=brief
                )
                interventions = InterventionStore(session)
                await interventions.lock_control(episode_id)
                await interventions.update_control(
                    episode_id, ControlChange(mode=InterventionMode.AUTO_INTERVENABLE)
                )
                await session.commit()
            except IntegrityError:
                # 並行する同じ command_id が先に commit した。その行を読み直して比べる
                await session.rollback()
                async with self._session_factory() as again:
                    return await self._replay(again, episode_id, brief, command_id)
        return DraftRecord(episode_id=episode_id, created=True)
