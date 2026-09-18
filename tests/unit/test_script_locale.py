"""台本の locale と形式（ADR-0026）。

- locale の唯一の宣言元は ``contracts.topic_planning.SCRIPT_LOCALES``。plan のある Episode は
  ``StrategyProfile.language``、無い Episode は ``DEFAULT_SCRIPT_LOCALE``
- prompt = f(locale) + content profile のデータ。言語と形式は独立
- plan の列（topic / subject / angle / era / hook）は生成器へ渡る prompt に明示的に載る
- locale・plan・形式は ``script_input_hash`` に入る（別 locale の台本を再利用しない）
"""

from __future__ import annotations

import json
import re
from datetime import date
from itertools import product
from typing import Any

import pytest

from contracts.artifacts import ScriptArtifact, parse_script_artifact
from contracts.states import ArtifactType
from contracts.topic_planning import (
    CONTENT_PROFILES,
    DEFAULT_CONTENT_PROFILE_ID,
    DEFAULT_SCRIPT_LOCALE,
    SCRIPT_LOCALES,
    STRATEGY_PROFILES,
    AnalyticsMode,
    DuplicateLevel,
    StrategyProfile,
)
from domain.errors import PromptContractError
from domain.script.brief import PLAN_FIELDS, legacy_brief, plan_brief
from domain.script.identity import script_input_hash
from infrastructure.db.repositories import (
    EpisodeRepository,
    NewTopicCandidate,
    NewTopicPlan,
    TopicPlanRepository,
)
from infrastructure.storage.memory_store import InMemoryArtifactStore
from prompts import load_prompt_template
from prompts.script import (
    SCRIPT_PROMPT_TEMPLATES,
    render_localized_script_prompt,
    script_prompt_template,
)
from tests.support.fakes import FakeStoryGenerator
from workers.planning.activities import CreateJobRequest, GenerateScriptRequest, ScriptActivities

EN_STRATEGY = "us_young_history_v1"
_JAPANESE = re.compile(r"[぀-ヿ一-鿿]")


def _script_json(language: str) -> str:
    narration = (
        ("Samurai carried two swords.", "One was for battle.", "The other said who you were.")
        if language == "en"
        else ("武士は刀を二本差した。", "一本は戦のため。", "もう一本は身分の証だった。")
    )
    return json.dumps(
        {
            "language": language,
            "title": "Two swords" if language == "en" else "二本差し",
            "hook": "Why two swords?" if language == "en" else "なぜ二本なのか",
            "scenes": [
                {"id": f"s{i}", "narration": n, "visual": "close-up", "duration_ms": 10_000}
                for i, n in enumerate(narration, 1)
            ],
        },
        ensure_ascii=False,
    )


def _render(locale: str, content_profile_id: str, subject_matter: dict[str, str]) -> str:
    content = CONTENT_PROFILES[content_profile_id]
    lo, hi = content.script_duration_seconds
    return render_localized_script_prompt(
        locale=locale,
        language=SCRIPT_LOCALES[locale].artifact_language,
        subject_matter_json=json.dumps(subject_matter, ensure_ascii=False),
        format_brief=content.format_brief,
        duration_min_seconds=lo,
        duration_max_seconds=hi,
        schema_json='{"type": "object"}',
    )


# ------------------------------------------------------------- SSoT / registry


def test_prompt_registry_covers_exactly_the_declared_locales() -> None:
    assert set(SCRIPT_PROMPT_TEMPLATES) == set(SCRIPT_LOCALES)
    for locale in SCRIPT_LOCALES:
        load_prompt_template(script_prompt_template(locale).template_name)


def test_every_strategy_language_is_a_script_locale() -> None:
    for profile in STRATEGY_PROFILES.values():
        assert profile.language in SCRIPT_LOCALES
    assert STRATEGY_PROFILES[EN_STRATEGY].language == "en-US"


def test_strategy_profile_rejects_an_unregistered_language() -> None:
    data = STRATEGY_PROFILES[EN_STRATEGY].model_dump()
    with pytest.raises(ValueError, match="language"):
        StrategyProfile.model_validate(dict(data, language="fr-FR"))


def test_artifact_language_of_every_locale_is_accepted_by_the_script_contract() -> None:
    for locale in SCRIPT_LOCALES.values():
        artifact = ScriptArtifact.model_validate(
            json.loads(_script_json(locale.artifact_language))
            | {
                "episode_id": "ep",
                "type": ArtifactType.SCRIPT.value,
                "schema_version": "1.0",
                "language": locale.artifact_language,
                "metadata": {"topic": "t", "generator": "g", "generator_model": "m"},
            }
        )
        assert artifact.language == locale.artifact_language


def test_default_locale_is_japanese_and_legacy_duration_is_preserved() -> None:
    assert DEFAULT_SCRIPT_LOCALE == "ja-JP"
    brief = legacy_brief("応仁の乱")
    assert brief.locale.locale == "ja-JP"
    assert brief.locale.artifact_language == "ja"
    assert brief.content_profile.content_profile_id == DEFAULT_CONTENT_PROFILE_ID
    assert brief.subject_matter == {"topic": "応仁の乱"}
    assert brief.topic_plan_id is None
    # ADR-0026 以前の prompt の「30〜45秒」は shorts profile のデータになった
    assert "30〜45秒" in _render("ja-JP", "shorts", brief.subject_matter)


def test_unknown_locale_has_no_template() -> None:
    with pytest.raises(ValueError):
        script_prompt_template("fr-FR")


def test_plan_brief_refuses_unknown_profiles_instead_of_falling_back() -> None:
    fields = dict.fromkeys(PLAN_FIELDS, "x")
    with pytest.raises(PromptContractError):
        plan_brief(
            topic_plan_id="p", strategy_profile_id="nope", content_profile_id="shorts", **fields
        )
    with pytest.raises(PromptContractError):
        plan_brief(
            topic_plan_id="p", strategy_profile_id=EN_STRATEGY, content_profile_id="nope", **fields
        )


# ------------------------------------------------------------- language × format


def test_en_us_template_is_english_and_ja_jp_template_is_japanese() -> None:
    en = load_prompt_template(script_prompt_template("en-US").template_name)
    ja = load_prompt_template(script_prompt_template("ja-JP").template_name)
    assert not _JAPANESE.search(en)
    assert "American English" in en
    assert "コードフェンス" in ja


@pytest.mark.parametrize("name", ["script/en-US", "script/ja-JP"])
def test_templates_do_not_hardcode_a_format(name: str) -> None:
    template = load_prompt_template(name)
    for token in ("Shorts", "shorts", "60 seconds", "30〜45", "縦型"):
        assert token not in template


@pytest.mark.parametrize(("locale", "content_id"), list(product(SCRIPT_LOCALES, CONTENT_PROFILES)))
def test_language_and_format_are_independent(locale: str, content_id: str) -> None:
    text = _render(locale, content_id, {"topic": "T"})
    content = CONTENT_PROFILES[content_id]
    lo, hi = content.script_duration_seconds
    assert content.format_brief in text
    assert str(lo) in text and str(hi) in text
    for other_id, other in CONTENT_PROFILES.items():
        if other_id != content_id:
            assert other.format_brief not in text
    assert f"`{SCRIPT_LOCALES[locale].artifact_language}`" in text
    assert (_JAPANESE.search(text) is not None) == (locale == "ja-JP")


@pytest.mark.parametrize(("locale", "content_id"), list(product(SCRIPT_LOCALES, CONTENT_PROFILES)))
def test_plan_brief_picks_locale_from_strategy_and_format_from_content_profile(
    monkeypatch: pytest.MonkeyPatch, locale: str, content_id: str
) -> None:
    strategy = STRATEGY_PROFILES[EN_STRATEGY].model_copy(
        update={"strategy_id": f"test_{locale}", "language": locale}
    )
    monkeypatch.setitem(STRATEGY_PROFILES, strategy.strategy_id, strategy)
    brief = plan_brief(
        topic_plan_id="p1",
        strategy_profile_id=strategy.strategy_id,
        content_profile_id=content_id,
        **dict.fromkeys(PLAN_FIELDS, "x"),
    )
    assert brief.locale is SCRIPT_LOCALES[locale]
    assert brief.content_profile is CONTENT_PROFILES[content_id]


# ------------------------------------------------------------- activity（SQLite + fake）


def _new_plan(content_profile_id: str = "shorts") -> NewTopicPlan:
    return NewTopicPlan(
        plan_date=date(2026, 9, 18),
        strategy_profile_id=EN_STRATEGY,
        strategy_version="1",
        content_profile_id=content_profile_id,
        content_profile_version="1",
        topic="Why samurai wore two swords",
        subject="daisho",
        angle="reason",
        era="edo",
        theme="warriors_and_war",
        hook="Two swords, one rule nobody explains",
        entities=["samurai", "daisho"],
        score=0.7,
        score_breakdown={"novelty": 1.0},
        duplicate_score=0.1,
        duplicate_level=DuplicateLevel.NONE,
        analytics_mode=AnalyticsMode.NO_ANALYTICS,
        analytics_confidence=0.0,
        planner_version="topic-planner-1",
        prompt_version="topic_en@2",
    )


async def _episode(session_factory: Any, *, with_plan: bool) -> tuple[str, Any]:
    async with session_factory() as session:
        plan = None
        if with_plan:
            saved = await TopicPlanRepository(session).save_plan(
                _new_plan(),
                [
                    NewTopicCandidate(
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
                ],
                selected_ordinal=0,
            )
            plan = saved.plan
        episode = await EpisodeRepository(session).create(
            topic=plan.topic if plan else "応仁の乱",
            topic_plan_id=plan.id if plan else None,
        )
        await session.commit()
        return episode.id, plan


async def _generate(session_factory: Any, episode_id: str, generator: FakeStoryGenerator):
    store = InMemoryArtifactStore()
    activities = ScriptActivities(
        session_factory=session_factory,
        store=store,
        generator=generator,
        bucket="artifacts",
        generator_id="fake",
        model="fake-model",
        timeout_seconds=5,
    )
    job_id = await activities.create_script_job(
        CreateJobRequest(episode_id=episode_id, max_attempts=3)
    )
    result = await activities.generate_script(
        GenerateScriptRequest(episode_id=episode_id, job_id=job_id, round=1)
    )
    return result, store


async def test_en_us_plan_reaches_the_generator_as_an_english_prompt(session_factory) -> None:
    episode_id, plan = await _episode(session_factory, with_plan=True)
    generator = FakeStoryGenerator(output=_script_json("en"))
    result, store = await _generate(session_factory, episode_id, generator)

    prompt = generator.requests[0].prompt
    assert "American English" in prompt
    assert "コードフェンス" not in prompt
    for name in PLAN_FIELDS:
        assert json.dumps(getattr(plan, name)) in prompt, name
    assert CONTENT_PROFILES["shorts"].format_brief in prompt

    artifact = parse_script_artifact(await store.get_json(result.object_key))
    assert artifact.language == "en"
    assert artifact.metadata.topic == plan.topic


async def test_episode_without_plan_uses_the_default_japanese_prompt(session_factory) -> None:
    episode_id, _ = await _episode(session_factory, with_plan=False)
    # LLM の自己申告の言語は使わない（locale が決める）
    generator = FakeStoryGenerator(output=_script_json("ja").replace('"ja"', '"en"', 1))
    result, store = await _generate(session_factory, episode_id, generator)

    prompt = generator.requests[0].prompt
    assert "応仁の乱" in prompt
    assert "コードフェンス" in prompt
    assert "30〜45秒" in prompt
    assert '"subject"' not in prompt
    artifact = parse_script_artifact(await store.get_json(result.object_key))
    assert artifact.language == "ja"


@pytest.mark.parametrize(
    "change",
    [{"locale": "en-US"}, {"topic_plan_id": "plan-1"}, {"content_profile": "long_form@1"}],
)
def test_input_hash_separates_locale_plan_and_format(change: dict[str, str]) -> None:
    base: dict[str, Any] = {
        "episode_id": "ep",
        "topic": "t",
        "artifact_type": "script",
        "target_schema_version": "1.0",
        "prompt_template_id": "script.ja-JP",
        "prompt_template_version": "1",
        "generator_id": "fake:m",
        "locale": "ja-JP",
        "topic_plan_id": None,
        "content_profile": "shorts@1",
    }
    assert script_input_hash(**base) != script_input_hash(**(base | change))


async def test_rerun_with_the_same_locale_reuses_the_artifact(session_factory) -> None:
    en_id, _ = await _episode(session_factory, with_plan=True)
    first = FakeStoryGenerator(output=_script_json("en"))
    await _generate(session_factory, en_id, first)
    # 同じ Episode の再実行は同じ入力 → 生成器を呼ばない（ADR-0012 のまま）
    again = FakeStoryGenerator(output=_script_json("en"))
    result, _ = await _generate(session_factory, en_id, again)
    assert result.reused
    assert again.calls == 0
