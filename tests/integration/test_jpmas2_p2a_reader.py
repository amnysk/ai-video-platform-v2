"""StoreBackedEpisodeReader を実 PostgreSQL + 実 MinIO で確かめる（ADR-0044 §2）。

SQLite の単体試験（tests/unit/test_episode_reader.py）と同じ主張を、実 DB の
timestamptz・UUID 型、実 bucket の JSON で言い直す。理由は
docs/testing/jpmas2-p2a-service-rationale.md。安全: 使い捨てスキーマ、``TEST_MINIO_BUCKET``。
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.errors import OperationError, OperationErrorCode
from contracts.agentic_pipeline import PinWorkflowPlanRequest, PipelineKind
from contracts.intervention import ActorType, ChangeKind
from contracts.pipeline import pipeline_workflow_id
from infrastructure.application.episode_reader import StoreBackedEpisodeReader
from infrastructure.db.models import Base
from infrastructure.intervention.store import Actor, InterventionStore
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.intervention import make_episode, seed_script_and_storyboard
from workers.pipeline.agentic_activities import AgenticPipelineActivities

TEST_DATABASE_URL = require_test_database_url()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not os.environ.get("MINIO_ENDPOINT"),
    reason="TEST_DATABASE_URL (*_test) and MINIO_ENDPOINT are required",
)


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"p2a_it_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        TEST_DATABASE_URL or "", connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def minio_store():
    from tests.support.minio import connect_test_artifact_store

    return await connect_test_artifact_store()


async def test_reader_against_real_postgres_and_minio(pg_session_factory, minio_store) -> None:
    reader = StoreBackedEpisodeReader(pg_session_factory, lambda: minio_store)
    async with pg_session_factory() as s:
        legacy = uuid.UUID(await make_episode(s))
        agentic = uuid.UUID(await make_episode(s))
        await seed_script_and_storyboard(s, minio_store, str(legacy))
    acts = AgenticPipelineActivities(
        session_factory=pg_session_factory, store=minio_store, bucket="artifacts"
    )
    await acts.pin_workflow_plan(
        dataclasses.replace(
            PinWorkflowPlanRequest(
                episode_id="", template_id="shorts_agentic", template_version="1"
            ),
            episode_id=str(agentic),
        )
    )

    assert await reader.pipeline_kind(agentic) is PipelineKind.AGENTIC
    first = await reader.list_episodes(limit=1, before=None)
    assert first.next_before is not None and len(first.episodes) == 1
    rest = await reader.list_episodes(limit=10, before=first.next_before)
    assert {e.episode_id for e in first.episodes + rest.episodes} == {legacy, agentic}

    ws = await reader.workspace(legacy)
    assert ws.workflow_id == pipeline_workflow_id(str(legacy))
    board = (await reader.storyboard(legacy)).storyboard
    assert board is not None and len(board.scenes) == 4

    async with pg_session_factory() as s:
        cs = await InterventionStore(s).insert_change_set(
            episode_id=legacy,
            kind=ChangeKind.SCENE_OVERRIDE_VISUAL,
            base_revision_id=board.revision_id,
            proposal={"scene_id": "sb1"},
            actor=Actor(ActorType.AI_ASSISTANT, "ai"),
            proposed_by_command_id="cmd-p2a-reader-01",
            estimated_cost_usd=Decimal("0.3"),
        )
        await s.commit()
    preview = await reader.change_impact(legacy, cs.change_set_id)
    assert preview.estimated_cost_usd == "0.3000" and preview.blockers == ()
    with pytest.raises(OperationError) as err:
        await reader.change_impact(agentic, cs.change_set_id)
    assert err.value.code is OperationErrorCode.CHANGE_SET_NOT_FOUND
    assert (await reader.progress(legacy)).failure is None
    assert (await reader.quality_review(legacy)).status == "not_reviewed"
