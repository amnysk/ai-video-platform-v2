"""台本生成に渡す「誰に・何語で・どの形式で・何を」（ADR-0026）。純粋関数のみ。

- locale（言語）と content profile（形式）は**独立**に決まる。prompt = f(locale) + 形式データ
- plan のある Episode: locale は ``STRATEGY_PROFILES[plan.strategy_profile_id].language``、
  形式は ``CONTENT_PROFILES[plan.content_profile_id]``、題材は plan の列を明示的に渡す
- plan の無い Episode（手動 API・ADR-0025 以前）: ``DEFAULT_SCRIPT_LOCALE`` と
  ``DEFAULT_CONTENT_PROFILE_ID``、題材は topic だけ

plan が指す profile / locale が登録に無いときは ``PromptContractError``（needs_input・
Temporal は retry しない）。黙って既定の locale / 形式へ落とさない。
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.topic_planning import (
    CONTENT_PROFILES,
    DEFAULT_CONTENT_PROFILE_ID,
    DEFAULT_SCRIPT_LOCALE,
    SCRIPT_LOCALES,
    STRATEGY_PROFILES,
    ContentProfile,
    ScriptLocale,
)
from domain.errors import PromptContractError

__all__ = ["PLAN_FIELDS", "ScriptBrief", "legacy_brief", "plan_brief"]

#: plan から台本 prompt へ渡す列（この順で prompt に載る）
PLAN_FIELDS: tuple[str, ...] = ("topic", "subject", "angle", "era", "hook")


@dataclass(frozen=True)
class ScriptBrief:
    locale: ScriptLocale
    content_profile: ContentProfile
    #: None なら plan の無い Episode（legacy）
    topic_plan_id: str | None
    #: prompt に**データとして**渡す題材。legacy は ``{"topic": ...}`` だけ
    subject_matter: dict[str, str]

    @property
    def topic(self) -> str:
        return self.subject_matter["topic"]


def legacy_brief(topic: str) -> ScriptBrief:
    return ScriptBrief(
        locale=SCRIPT_LOCALES[DEFAULT_SCRIPT_LOCALE],
        content_profile=CONTENT_PROFILES[DEFAULT_CONTENT_PROFILE_ID],
        topic_plan_id=None,
        subject_matter={"topic": topic},
    )


def plan_brief(
    *,
    topic_plan_id: str,
    strategy_profile_id: str,
    content_profile_id: str,
    topic: str,
    subject: str,
    angle: str,
    era: str,
    hook: str,
) -> ScriptBrief:
    strategy = STRATEGY_PROFILES.get(strategy_profile_id)
    if strategy is None:
        raise PromptContractError(f"unknown strategy profile: {strategy_profile_id!r}")
    content = CONTENT_PROFILES.get(content_profile_id)
    if content is None:
        raise PromptContractError(f"unknown content profile: {content_profile_id!r}")
    locale = SCRIPT_LOCALES.get(strategy.language)
    if locale is None:  # StrategyProfile の validator が防ぐが、黙って既定へ落とさない
        raise PromptContractError(f"unknown script locale: {strategy.language!r}")
    values = {"topic": topic, "subject": subject, "angle": angle, "era": era, "hook": hook}
    return ScriptBrief(
        locale=locale,
        content_profile=content,
        topic_plan_id=topic_plan_id,
        subject_matter={k: values[k] for k in PLAN_FIELDS},
    )
