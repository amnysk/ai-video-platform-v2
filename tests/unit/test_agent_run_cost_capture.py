"""``RunCapture`` の費用と agent_spec（ADR-0043 §2、INV-56）。SQLite + InMemoryArtifactStore。"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

from contracts.intervention import AgentRole
from infrastructure.db.models import AgentRunRow
from infrastructure.db.repositories import EpisodeRepository
from infrastructure.intervention.capture import AgentRunRecorder, PromptSource, RunCapture
from infrastructure.storage.memory_store import InMemoryArtifactStore


async def _episode(session) -> str:
    episode = await EpisodeRepository(session).create(topic="t")
    await session.commit()
    return episode.id


def _capture(episode_id: str, key: str, **extra) -> RunCapture:
    return RunCapture(
        idempotency_key=key,
        role=AgentRole.STRATEGY,
        step="strategy",
        model="m",
        prompt_version="strategy-v1",
        prompt="P",
        prompt_source=PromptSource.CAPTURED,
        input_refs={},
        request=None,
        response=None,
        episode_id=episode_id,
        **extra,
    )


async def _row(session, run_id: str) -> AgentRunRow:
    row = await session.get(AgentRunRow, uuid.UUID(run_id))
    assert row is not None
    return row


async def test_a_legacy_capture_keeps_null_cost_and_adds_nothing_to_the_snapshot(session) -> None:
    episode_id = await _episode(session)
    store = InMemoryArtifactStore()
    run = await AgentRunRecorder(store).record(session, _capture(episode_id, "legacy"))
    await session.commit()
    assert (await _row(session, run.agent_run_id)).estimated_cost_usd is None
    assert "agent_spec" not in json.loads(await store.get_bytes(run.snapshot_object_key))


async def test_an_unknown_cost_stays_null_not_zero(session) -> None:
    episode_id = await _episode(session)
    run = await AgentRunRecorder(InMemoryArtifactStore()).record(
        session, _capture(episode_id, "unknown", estimated_cost_usd=None, token_usage=None)
    )
    await session.commit()
    row = await _row(session, run.agent_run_id)
    assert row.estimated_cost_usd is None and row.token_usage is None


async def test_a_known_cost_and_the_agent_spec_are_stored(session) -> None:
    episode_id = await _episode(session)
    store = InMemoryArtifactStore()
    spec = {
        "role": "strategy",
        "spec_version": "1.0",
        "manifest_hash": "h" * 64,
        "model_policy_ref": "codex_cli_text@1",
    }
    run = await AgentRunRecorder(store).record(
        session,
        _capture(
            episode_id,
            "known",
            estimated_cost_usd=Decimal("0.0123"),
            agent_spec=spec,
            token_usage={"input_tokens": 1, "output_tokens": 2, "llm_calls": 1},
        ),
    )
    await session.commit()
    row = await _row(session, run.agent_run_id)
    assert Decimal(str(row.estimated_cost_usd)) == Decimal("0.0123")
    assert json.loads(await store.get_bytes(run.snapshot_object_key))["agent_spec"] == spec
