"""Agent への入力 document の組み立て（ADR-0043 §6）。SQLite + InMemoryArtifactStore。

各テストの理由: docs/testing/jpmas2-p1b-rationale.md
"""

from __future__ import annotations

import ast
import inspect
from datetime import date
from pathlib import Path

import infrastructure.agents.inputs as inputs_module
from contracts.agentic import Lesson
from contracts.agents import AgentHandlerRef
from contracts.states import ArtifactType
from domain.agents.runtime import AGENT_INPUT_NAMES
from infrastructure.agents.inputs import (
    COLLECTOR_NOT_IMPLEMENTED,
    NoPublicMetrics,
    _Builder,
    assemble_agent_inputs,
    is_missing,
    missing_document,
    required_inputs,
)
from infrastructure.db.repositories import AnalyticsSnapshotRepository, EpisodeRepository
from tests.support.agent_stage import adopt_candidate, make_world, user_brief
from tests.support.intervention import store_artifact


def _lesson(episode_id: str, status: str) -> dict:
    return Lesson.model_validate(
        {
            "episode_id": episode_id,
            "video_id": "abc",
            "scope": "shorts about edo",
            "status": status,
            "measurement_windows": [{"window": "48h", "missing_reason": "not collected"}],
            "observations": ["views were low"],
        }
    ).model_dump(mode="json")


async def _assemble(world, ref, **kw):
    return await assemble_agent_inputs(
        session_factory=world.session_factory,
        store=world.store,
        episode_id=world.episode_id,
        handler_ref=ref,
        **kw,
    )


def test_every_input_name_has_a_builder() -> None:
    """handler の入力名（正本）に、組み立てる側が漏れなく対応する。"""
    names = {n for names in AGENT_INPUT_NAMES.values() for n in names}
    for name in names:
        assert inspect.iscoroutinefunction(getattr(_Builder, name)), name


def test_required_inputs_are_inputs_of_the_handler() -> None:
    for ref in AgentHandlerRef:
        assert set(required_inputs(ref)) <= set(AGENT_INPUT_NAMES[ref])


def test_inputs_come_only_from_the_canonical_stores() -> None:
    """検索 index（OpenSearch）は読まない: 正本は PostgreSQL と ArtifactStore だけ。"""
    tree = ast.parse(Path(inputs_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {m for m in imported if "opensearch" in m.lower() or "logging" in m}


async def test_strategy_inputs_cover_exactly_the_handler_names(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    assembled = await _assemble(world, AgentHandlerRef.STRATEGY_AGENT_V1)
    assert set(assembled.documents) == set(AGENT_INPUT_NAMES[AgentHandlerRef.STRATEGY_AGENT_V1])
    assert assembled.documents["user_brief"]["theme"] == user_brief()["theme"]
    assert set(assembled.input_revisions) == {"user_brief"}


async def test_without_analytics_measured_is_false_and_nothing_is_invented(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    assembled = await _assemble(world, AgentHandlerRef.STRATEGY_AGENT_V1)
    assert assembled.documents["analytics"] == {
        "measured": False,
        "reason": "no_analytics_snapshot",
    }


async def test_analytics_summarises_the_latest_existing_snapshot(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    payload = {
        "videos_by_window": {
            "28": [
                {"video_id": "a", "views": 100.0, "subscribers_gained": 2.0},
                {"video_id": "b", "views": 0.0, "subscribers_gained": 0.0},
            ]
        },
        "audience": {"country_us": 0.4},
    }
    async with session_factory() as session:
        await AnalyticsSnapshotRepository(session).save(
            date(2026, 10, 1), "youtube_analytics", payload
        )
        await session.commit()
    assembled = await _assemble(world, AgentHandlerRef.STRATEGY_AGENT_V1)
    analytics = assembled.documents["analytics"]
    assert analytics["measured"] is True
    assert analytics["videos_with_views"] == 1  # views のある動画だけ数える
    assert analytics["windows"]["28d"]["views"] == 100.0
    assert "analytics_snapshot" in assembled.extra_refs


async def test_lessons_of_other_episodes_come_with_their_status_and_own_lessons_do_not(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    other = await make_world(session_factory, artifact_store, brief=False)
    async with session_factory() as session:
        for episode_id, status in ((other.episode_id, "proposed"), (world.episode_id, "validated")):
            await store_artifact(
                session,
                artifact_store,
                episode_id,
                ArtifactType.LESSON,
                _lesson(episode_id, status),
            )
    assembled = await _assemble(world, AgentHandlerRef.STRATEGY_AGENT_V1)
    (item,) = assembled.documents["lessons"]["items"]
    assert item["episode_id"] == other.episode_id
    assert item["status"] == "proposed"  # 解釈（仮説扱い）は handler の決定論
    assert item["revision_id"]


async def test_recent_titles_are_other_episodes_topics(session_factory, artifact_store) -> None:
    world = await make_world(session_factory, artifact_store)
    async with session_factory() as session:
        await EpisodeRepository(session).create(topic="The fall of Edo castle")
        await session.commit()
    assembled = await _assemble(world, AgentHandlerRef.STRATEGY_AGENT_V1)
    titles = assembled.documents["recent_titles"]["titles"]
    assert "The fall of Edo castle" in titles
    assert len(titles) == len(set(titles))


async def test_producer_decision_is_the_latest_choose_topic(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store, strategy=True)
    before = await _assemble(world, AgentHandlerRef.CREATIVE_DIRECTOR_V1)
    assert is_missing(before.documents["producer_decision"])
    await adopt_candidate(session_factory, world.episode_id, "c2")
    after = await _assemble(world, AgentHandlerRef.CREATIVE_DIRECTOR_V1)
    assert after.documents["producer_decision"] == {"adopted_candidate_id": "c2"}
    assert after.documents["strategy_brief"]["recommended_candidate_id"] == "c1"
    assert set(after.input_revisions) == {"user_brief", "strategy_brief"}


async def test_missing_inputs_use_one_uniform_representation(
    session_factory, artifact_store
) -> None:
    """欠けた入力は ``{"missing": True, "reason": ...}``。空 dict・0・空文字にしない。"""
    world = await make_world(session_factory, artifact_store)
    assembled = await _assemble(world, AgentHandlerRef.QUALITY_REVIEWER_V1)
    for name in ("script", "storyboard", "final_video"):
        assert assembled.documents[name] == missing_document(f"no_current_{name}")
    assert set(assembled.input_revisions) == set()
    assert set(assembled.missing()) >= {"final_video", "script", "storyboard"}


async def test_quality_inputs_include_the_deterministic_review_and_the_manifest(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store, storyboard=True)
    assembled = await _assemble(world, AgentHandlerRef.QUALITY_REVIEWER_V1)
    review = assembled.documents["rule_review"]
    assert review["verdict"] in {"pass", "needs_attention"}
    assert assembled.documents["manifest"]["manifest_id"] == review["manifest_id"]
    assert assembled.documents["storyboard"]["scenes"]
    assert {"storyboard", "script"} <= set(assembled.input_revisions)
    # 完成動画が無いことは欠測として明示される（rule_review は所見として別に持つ）
    assert is_missing(assembled.documents["final_video"])


async def test_the_default_measurements_say_the_collector_is_not_implemented(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    assembled = await _assemble(world, AgentHandlerRef.LEARNING_AGENT_V1)
    assert assembled.documents["measurements"] == {
        "public": False,
        "windows": [],
        "reason": COLLECTOR_NOT_IMPLEMENTED,
    }
    assert is_missing(assembled.documents["upload_receipt"])
    assert assembled.documents["baseline"]["available"] is False


async def test_an_injected_metrics_port_supplies_the_measurements(
    session_factory, artifact_store
) -> None:
    class Mock:
        async def measurements(self, *, episode_id: str, video_id: str | None):
            return {"public": True, "windows": [{"window": "48h", "views": 10}], "video": video_id}

    world = await make_world(session_factory, artifact_store)
    assembled = await _assemble(world, AgentHandlerRef.LEARNING_AGENT_V1, metrics=Mock())
    assert assembled.documents["measurements"]["public"] is True


async def test_no_public_metrics_never_reports_public(session_factory) -> None:
    assert (await NoPublicMetrics().measurements(episode_id="e", video_id="v"))["public"] is False
