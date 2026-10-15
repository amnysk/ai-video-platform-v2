"""台本の再利用キーに CreativeBrief の版を入れる（ADR-0043 §Consequences の負債の解消）。

brief が変わったのに古い brief で作った台本を採用しない。plan のある Episode・brief の無い
Episode の ``input_hash`` は従来と同一（INV-55）。
各テストの理由: docs/testing/jpmas2-fixa-rationale.md
"""

from __future__ import annotations

from typing import Any

from contracts.states import JobType
from domain.script.identity import script_input_hash, with_brief_revision
from infrastructure.agents.episode_artifacts import write_episode_artifact
from infrastructure.db.repositories import JobRepository
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.fakes import FakeStoryGenerator
from tests.unit.test_script_creative_brief import (
    BUCKET,
    _activities,
    _creative,
    _plain_episode,
    _script_json,
)
from workers.planning.activities import CreateJobRequest, GenerateScriptRequest

_BASE: dict[str, Any] = {
    "episode_id": "e",
    "topic": "t",
    "artifact_type": "script",
    "target_schema_version": "1.0",
    "prompt_template_id": "p",
    "prompt_template_version": "1",
    "generator_id": "g",
    "locale": "en",
    "topic_plan_id": None,
    "content_profile": "c@1",
}


def test_no_brief_revision_keeps_the_hash_and_a_revision_changes_it() -> None:
    base = script_input_hash(**_BASE)
    assert with_brief_revision(base, None) == base
    a = with_brief_revision(base, "rev-a")
    assert a != base and len(a) == 64
    assert with_brief_revision(base, "rev-b") != a
    assert with_brief_revision(base, "rev-a") == a


async def _generate(acts: Any, episode_id: str, session_factory: Any = None) -> Any:
    job_id = await acts.create_script_job(CreateJobRequest(episode_id=episode_id, max_attempts=3))
    if session_factory is not None:  # 成功済みの job は再開できないので、再生成には新しい job
        async with session_factory() as s:
            job = await JobRepository(s).create(
                episode_id=episode_id, type=JobType.WRITE_SCRIPT, max_attempts=3
            )
            await s.commit()
            job_id = job.id
    return await acts.generate_script(
        GenerateScriptRequest(episode_id=episode_id, job_id=job_id, round=1)
    )


async def _write_brief(session_factory: Any, store: Any, episode_id: str, **over: Any) -> None:
    async with session_factory() as s:
        await write_episode_artifact(
            s, store, bucket=BUCKET, episode_id=episode_id, model=_creative(episode_id, **over)
        )
        await s.commit()


async def test_a_script_made_under_an_older_creative_brief_is_not_adopted(session_factory) -> None:
    store = InMemoryArtifactStore()
    generator = FakeStoryGenerator(output=_script_json())
    acts = _activities(session_factory, store, generator)
    episode_id = await _plain_episode(session_factory, "draft")
    await _write_brief(session_factory, store, episode_id)
    first = await _generate(acts, episode_id)
    assert first.reused is False and len(generator.requests) == 1

    again = await _generate(acts, episode_id)  # brief 不変: 再利用（二重生成しない）
    assert again.reused is True and len(generator.requests) == 1

    await _write_brief(session_factory, store, episode_id, hook="A different opening line here.")
    changed = await _generate(
        acts, episode_id, session_factory
    )  # brief が変わった: 古い台本は採用しない
    assert changed.reused is False and len(generator.requests) == 2
