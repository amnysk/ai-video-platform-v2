"""台本の brief 決定: TopicPlan が無く現行の creative_brief がある Episode（ADR-0043 §6）。

plan のある Episode・creative_brief の無い Episode は従来と同一（INV-55）。
各テストの理由: docs/testing/jpmas2-p1c-rationale.md
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from contracts.agentic import CreativeBrief
from contracts.states import ArtifactType
from contracts.topic_planning import (
    DEFAULT_CONTENT_PROFILE_ID,
    DEFAULT_STRATEGY_PROFILE_ID,
    DuplicateLevel,
)
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.errors import PromptContractError
from domain.script.brief import PLAN_FIELDS, legacy_brief, plan_brief
from domain.script.identity import script_input_hash
from infrastructure.agents.episode_artifacts import write_episode_artifact
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    NewTopicCandidate,
    TopicPlanRepository,
)
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.fakes import FakeStoryGenerator
from tests.unit.test_script_locale import _new_plan
from workers.planning.activities import CreateJobRequest, GenerateScriptRequest, ScriptActivities

BUCKET = "artifacts"


def _creative(episode_id: str, **over: Any) -> CreativeBrief:
    base: dict[str, Any] = {
        "episode_id": episode_id,
        "adopted_candidate_id": "c1",
        "topic": "Why the Shogun hid in plain sight",
        "subject": "tokugawa_ieyasu",
        "angle": "reason",
        "era": "edo",
        "hook": "He ruled Japan without ever being king.",
        "strategy_profile_id": DEFAULT_STRATEGY_PROFILE_ID,
        "content_profile_id": DEFAULT_CONTENT_PROFILE_ID,
        "hook_options": ["He ruled Japan without ever being king."],
        "tone": "curious",
    }
    return CreativeBrief.model_validate({**base, **over})


def _new_candidate() -> NewTopicCandidate:
    return NewTopicCandidate(
        ordinal=0,
        round=1,
        payload={"topic": "t"},
        subject="daisho",
        angle="reason",
        duplicate_level=DuplicateLevel.NONE,
        duplicate_score=0.1,
        duplicate_of=None,
        score=0.7,
        score_breakdown={"novelty": 1.0},
        rejected=False,
    )


def _script_json() -> str:
    return json.dumps(
        {
            "language": "en",
            "title": "Hidden ruler",
            "hook": "Who ruled?",
            "scenes": [
                {"id": f"s{i}", "narration": n, "visual": "close-up", "duration_ms": 10_000}
                for i, n in enumerate(
                    (
                        "A shogun ruled in the emperor's name.",
                        "He held every real lever of power.",
                        "Yet the throne stayed in Kyoto.",
                    ),
                    1,
                )
            ],
        }
    )


def _activities(session_factory: Any, store: Any, generator: Any) -> ScriptActivities:
    return ScriptActivities(
        session_factory=session_factory,
        store=store,
        generator=generator,
        bucket=BUCKET,
        generator_id="fake",
        model="fake-model",
        timeout_seconds=5,
    )


async def _plain_episode(session_factory: Any, topic: str = "応仁の乱") -> str:
    async with session_factory() as s:
        ep = await EpisodeRepository(s).create(topic=topic)
        await s.commit()
        return ep.id


async def _brief_of(session_factory: Any, acts: ScriptActivities, episode_id: str):
    async with session_factory() as s:
        episode = await EpisodeRepository(s).get(episode_id)
        assert episode is not None
        return await acts._brief(s, episode_id, episode.topic, episode.topic_plan_id)


async def test_an_episode_with_a_creative_brief_and_no_plan_gets_its_subject_matter(
    session_factory,
) -> None:
    store = InMemoryArtifactStore()
    episode_id = await _plain_episode(session_factory, "draft")
    async with session_factory() as s:
        await write_episode_artifact(
            s, store, bucket=BUCKET, episode_id=episode_id, model=_creative(episode_id)
        )
        await s.commit()
    acts = _activities(session_factory, store, FakeStoryGenerator(output=_script_json()))
    brief = await _brief_of(session_factory, acts, episode_id)
    assert brief.topic_plan_id is None
    assert brief.subject_matter == {
        "topic": "Why the Shogun hid in plain sight",
        "subject": "tokugawa_ieyasu",
        "angle": "reason",
        "era": "edo",
        "hook": "He ruled Japan without ever being king.",
    }
    assert tuple(brief.subject_matter) == PLAN_FIELDS


async def test_the_creative_brief_reaches_the_generator_prompt(session_factory) -> None:
    store = InMemoryArtifactStore()
    episode_id = await _plain_episode(session_factory, "draft")
    async with session_factory() as s:
        await write_episode_artifact(
            s, store, bucket=BUCKET, episode_id=episode_id, model=_creative(episode_id)
        )
        await s.commit()
    generator = FakeStoryGenerator(output=_script_json())
    acts = _activities(session_factory, store, generator)
    job_id = await acts.create_script_job(CreateJobRequest(episode_id=episode_id, max_attempts=3))
    await acts.generate_script(GenerateScriptRequest(episode_id=episode_id, job_id=job_id, round=1))
    prompt = generator.requests[0].prompt
    assert "American English" in prompt
    for value in ("tokugawa_ieyasu", "edo", "He ruled Japan without ever being king."):
        assert json.dumps(value) in prompt


async def test_an_episode_without_a_creative_brief_keeps_the_legacy_brief_and_hash(
    session_factory,
) -> None:
    store = InMemoryArtifactStore()
    episode_id = await _plain_episode(session_factory)
    acts = _activities(session_factory, store, FakeStoryGenerator(output=_script_json()))
    brief = await _brief_of(session_factory, acts, episode_id)
    assert brief == legacy_brief("応仁の乱")

    def digest(b: Any) -> str:
        return script_input_hash(
            episode_id=episode_id,
            topic=b.topic,
            artifact_type=ArtifactType.SCRIPT.value,
            target_schema_version="1.0",
            prompt_template_id="t",
            prompt_template_version="1",
            generator_id="g",
            locale=b.locale.locale,
            topic_plan_id=b.topic_plan_id,
            content_profile=f"{b.content_profile.content_profile_id}@{b.content_profile.version}",
        )

    assert digest(brief) == digest(legacy_brief("応仁の乱"))


async def test_an_episode_with_a_plan_keeps_the_plan_brief_even_with_a_creative_brief(
    session_factory,
) -> None:
    store = InMemoryArtifactStore()
    async with session_factory() as s:
        saved = await TopicPlanRepository(s).save_plan(
            _new_plan(), [_new_candidate()], selected_ordinal=0
        )
        plan = saved.plan
        episode = await EpisodeRepository(s).create(topic=plan.topic, topic_plan_id=plan.id)
        await write_episode_artifact(
            s, store, bucket=BUCKET, episode_id=episode.id, model=_creative(episode.id)
        )
        await s.commit()
    acts = _activities(session_factory, store, FakeStoryGenerator(output=_script_json()))
    brief = await _brief_of(session_factory, acts, episode.id)
    assert brief == plan_brief(
        topic_plan_id=plan.id,
        strategy_profile_id=plan.strategy_profile_id,
        content_profile_id=plan.content_profile_id,
        topic=plan.topic,
        subject=plan.subject,
        angle=plan.angle,
        era=plan.era,
        hook=plan.hook,
    )


async def test_an_unsupported_creative_brief_version_is_refused_not_read(session_factory) -> None:
    store = InMemoryArtifactStore()
    episode_id = await _plain_episode(session_factory)
    document = {**_creative(episode_id).model_dump(mode="json"), "schema_version": "9.9"}
    async with session_factory() as s:
        digest = sha256_hex(canonical_json_bytes(document))
        key = artifact_object_key(episode_id, ArtifactType.CREATIVE_BRIEF.value, digest)
        await store.put_json(key, document)
        await ArtifactMetadataRepository(s).record(
            episode_id=episode_id,
            artifact_type=ArtifactType.CREATIVE_BRIEF,
            schema_version="9.9",
            bucket=BUCKET,
            object_key=key,
            sha256=digest,
        )
        await s.commit()
    acts = _activities(session_factory, store, FakeStoryGenerator(output=_script_json()))
    with pytest.raises(PromptContractError):
        await _brief_of(session_factory, acts, episode_id)
