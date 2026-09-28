"""拒否されたシーンの代替案を計画・保存する Activity（ADR-0035, INV-34）。

実 DB 相当（sqlite）と in-memory store。planner は fake。LLM も provider も呼ばない。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from contracts.artifacts import parse_scene_visual_override_artifact
from contracts.production_activities import PlanSceneAlternativeRequest
from contracts.states import ArtifactType, ProviderCall
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.errors import (
    SceneAlternativeInfeasibleError,
    SceneAlternativeInvalidError,
    SceneAlternativeLimitReachedError,
    SceneAlternativePlannerUnavailableError,
)
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    ProviderRejectionRepository,
    ProviderReservationRepository,
)
from tests.support.production import (
    LIKENESS_REJECTION,
    FakeSceneAlternativePlanner,
    sample_script,
    sample_storyboard,
)
from workers.production.scene_recovery_activities import SceneAlternativeActivities


async def _put(session_factory, store, episode_id, artifact_type, payload, scene_id=None):
    digest = sha256_hex(canonical_json_bytes(payload))
    put = await store.put_json(
        f"artifacts/{episode_id}/{artifact_type.value}/{digest}.json", payload
    )
    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=artifact_type,
            schema_version="1.0",
            bucket="b",
            object_key=put.key,
            sha256=digest,
            input_hash=digest,
            scene_id=scene_id,
        )
        await session.commit()
    return meta


async def _seed(session_factory, store, *, rejected_cost="0.97"):
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
    script_payload = sample_script(episode.id).model_dump(mode="json")
    script = await _put(session_factory, store, episode.id, ArtifactType.SCRIPT, script_payload)
    storyboard_payload = sample_storyboard(
        episode.id, script_artifact_id=script.id, script_sha256=script.sha256
    ).model_dump(mode="json")
    storyboard = await _put(
        session_factory, store, episode.id, ArtifactType.STORYBOARD, storyboard_payload
    )
    await _reject(session_factory, episode.id, "sb2", cost=rejected_cost)
    return episode.id, storyboard.id


async def _reject(session_factory, episode_id, scene_id, *, cost="0.97"):
    """動画の予約が spent + 拒否された状態（paid_job が残すのと同じ形）。"""
    async with session_factory() as session:
        reservations = ProviderReservationRepository(session)
        row = await reservations.reserve(
            episode_id=episode_id,
            provider=ProviderCall.FAL_VIDEO,
            idempotency_key=uuid.uuid4().hex,
            input_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            round=1,
            scene_id=scene_id,
            estimated_cost_usd=Decimal(cost),
        )
        await reservations.mark_dispatched(row.id)
        spent = await reservations.mark_spent(
            row.id,
            raw_output_key=None,
            reconciled_by="conservative",
            input_rejected_by_provider=True,
        )
        record = await ProviderRejectionRepository(session).record(
            episode_id=episode_id,
            scene_id=scene_id,
            provider=ProviderCall.FAL_VIDEO,
            reservation_id=spent.id,
            input_hash=spent.input_hash,
            rejection=LIKENESS_REJECTION,
            source_media_sha256="i" * 64,
        )
        await session.commit()
    return record


def _activities(session_factory, store, planner):
    return SceneAlternativeActivities(
        session_factory=session_factory, store=store, bucket="b", planner=planner
    )


def _request(episode_id, storyboard_id, *, scene_id="sb2", seen_revision=0):
    return PlanSceneAlternativeRequest(
        episode_id=episode_id,
        workflow_id="wf",
        run_id="run",
        scene_id=scene_id,
        storyboard_artifact_id=storyboard_id,
        seen_revision=seen_revision,
    )


async def test_plans_and_saves_an_alternative_for_the_rejected_scene_only(
    session_factory, artifact_store
) -> None:
    episode_id, storyboard_id = await _seed(session_factory, artifact_store)
    planner = FakeSceneAlternativePlanner()
    outcome = await _activities(session_factory, artifact_store, planner).plan(
        _request(episode_id, storyboard_id)
    )
    assert outcome.revision == 1 and outcome.newly_planned
    # planner は拒否の構造・ナレーション・人物を外した許可集合を受け取る
    (context,) = planner.contexts
    assert context.rejections[0].reason == "partner_validation_failed"
    assert "named_person" not in {s.value for s in context.allowed_subjects}
    assert context.narration  # 名前・功績はナレーション側に残る
    async with session_factory() as session:
        repo = ArtifactMetadataRepository(session)
        current = await repo.find_current_by_type(
            episode_id, ArtifactType.SCENE_VISUAL_OVERRIDE, "sb2"
        )
        others = await repo.find_current_by_type(
            episode_id, ArtifactType.SCENE_VISUAL_OVERRIDE, "sb1"
        )
        (planner_call,) = await ProviderReservationRepository(session).list_for_episode_provider(
            episode_id, ProviderCall.CODEX_SCENE_ALTERNATIVE
        )
    assert current is not None and current.id == outcome.override_artifact_id
    assert others is None
    override = parse_scene_visual_override_artifact(
        await artifact_store.get_json(current.object_key)
    )
    assert override.rationale and override.rejection_ids
    # planner 呼び出しは予約台帳に evidence 付きで残り、Artifact が紐づく
    assert planner_call.raw_output_key is not None
    assert planner_call.outcome_artifact_id == current.id


async def test_activity_retry_returns_the_saved_plan_without_calling_the_planner_again(
    session_factory, artifact_store
) -> None:
    episode_id, storyboard_id = await _seed(session_factory, artifact_store)
    planner = FakeSceneAlternativePlanner()
    activities = _activities(session_factory, artifact_store, planner)
    first = await activities.plan(_request(episode_id, storyboard_id))
    again = await activities.plan(_request(episode_id, storyboard_id))
    assert again.override_artifact_id == first.override_artifact_id
    assert not again.newly_planned and len(planner.contexts) == 1


async def test_blocked_again_on_the_same_plan_does_not_loop(
    session_factory, artifact_store
) -> None:
    """同じ案のまま再び止まった（新しい拒否が無い）なら、計画せず人の判断へ。"""
    episode_id, storyboard_id = await _seed(session_factory, artifact_store)
    planner = FakeSceneAlternativePlanner()
    activities = _activities(session_factory, artifact_store, planner)
    first = await activities.plan(_request(episode_id, storyboard_id))
    with pytest.raises(SceneAlternativeInvalidError, match="blocked again"):
        await activities.plan(_request(episode_id, storyboard_id, seen_revision=first.revision))
    assert len(planner.contexts) == 1


async def test_a_new_rejection_of_the_alternative_gets_a_second_plan(
    session_factory, artifact_store
) -> None:
    episode_id, storyboard_id = await _seed(session_factory, artifact_store)
    planner = FakeSceneAlternativePlanner()
    activities = _activities(session_factory, artifact_store, planner)
    first = await activities.plan(_request(episode_id, storyboard_id))
    await _reject(session_factory, episode_id, "sb2", cost="0.04")
    second = await activities.plan(_request(episode_id, storyboard_id, seen_revision=1))
    assert second.revision == 2 and second.override_artifact_id != first.override_artifact_id
    # 2回目の planner は1回目の案を「既に試した」として受け取る
    assert planner.contexts[1].previous[0].revision == 1


async def test_scene_limit_stops_automation(session_factory, artifact_store) -> None:
    """INV-34: 1シーンの上限（既定2回）に達したら、新しい拒否があっても計画しない。"""
    episode_id, storyboard_id = await _seed(session_factory, artifact_store, rejected_cost="0.01")
    planner = FakeSceneAlternativePlanner()
    activities = _activities(session_factory, artifact_store, planner)
    for revision in (0, 1):
        await activities.plan(_request(episode_id, storyboard_id, seen_revision=revision))
        await _reject(session_factory, episode_id, "sb2", cost="0.01")
    with pytest.raises(SceneAlternativeLimitReachedError, match="this scene"):
        await activities.plan(_request(episode_id, storyboard_id, seen_revision=2))
    assert len(planner.contexts) == 2


async def test_cost_cap_stops_automation_before_calling_the_planner(
    session_factory, artifact_store
) -> None:
    """INV-34: 復旧の追加費用が上限（既定 $5）を超える見込みなら計画しない。"""
    episode_id, storyboard_id = await _seed(session_factory, artifact_store, rejected_cost="4.99")
    planner = FakeSceneAlternativePlanner()
    with pytest.raises(SceneAlternativeLimitReachedError, match="cap"):
        await _activities(session_factory, artifact_store, planner).plan(
            _request(episode_id, storyboard_id)
        )
    assert planner.contexts == []


async def test_infeasible_plan_stops_with_the_planners_reason(
    session_factory, artifact_store
) -> None:
    episode_id, storyboard_id = await _seed(session_factory, artifact_store)
    planner = FakeSceneAlternativePlanner([{"feasible": False, "reason": "only his face matters"}])
    with pytest.raises(SceneAlternativeInfeasibleError, match="only his face matters"):
        await _activities(session_factory, artifact_store, planner).plan(
            _request(episode_id, storyboard_id)
        )


async def test_a_person_subject_after_a_likeness_rejection_is_not_saved(
    session_factory, artifact_store
) -> None:
    episode_id, storyboard_id = await _seed(session_factory, artifact_store)
    planner = FakeSceneAlternativePlanner(
        [
            {
                "feasible": True,
                "visual_kind": "generated",
                "visual_subject": "named_person",
                "visual_description": "Ieyasu seen from far away",
                "rationale": "he is small",
            }
        ]
    )
    with pytest.raises(SceneAlternativeInvalidError, match="not allowed"):
        await _activities(session_factory, artifact_store, planner).plan(
            _request(episode_id, storyboard_id)
        )
    async with session_factory() as session:
        assert (
            await ArtifactMetadataRepository(session).find_current_by_type(
                episode_id, ArtifactType.SCENE_VISUAL_OVERRIDE, "sb2"
            )
            is None
        )


async def test_no_planner_configured_is_needs_input(session_factory, artifact_store) -> None:
    episode_id, storyboard_id = await _seed(session_factory, artifact_store)
    with pytest.raises(SceneAlternativePlannerUnavailableError):
        await _activities(session_factory, artifact_store, None).plan(
            _request(episode_id, storyboard_id)
        )


async def test_scene_without_a_recorded_rejection_is_not_planned(
    session_factory, artifact_store
) -> None:
    episode_id, storyboard_id = await _seed(session_factory, artifact_store)
    with pytest.raises(SceneAlternativeInvalidError, match="no recorded provider rejection"):
        await _activities(session_factory, artifact_store, FakeSceneAlternativePlanner()).plan(
            _request(episode_id, storyboard_id, scene_id="sb1")
        )
