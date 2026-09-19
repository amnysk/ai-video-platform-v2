"""台本 prompt の locale 別 registry（ADR-0026）。

テンプレートは locale（言語）だけで選ぶ。形式（Shorts / Long）は content profile の
**データ**（``format_brief`` と ``script_duration_seconds``）として埋める。
workflow / activity のコードにファイル名を書かない。ここの鍵は
``contracts.topic_planning.SCRIPT_LOCALES`` の鍵と一致する（テストが検査する）。
テンプレートを変えたら、その locale の ``version`` を上げる（``script_input_hash`` に入る）。
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.topic_planning import ScriptLocale
from prompts import _render

#: prompt に例示するシーン尺（秒）。予算そのものは ``ScriptLocale`` が決める
NARRATION_BUDGET_EXAMPLE_SECONDS: tuple[int, ...] = (4, 6, 8, 10, 12, 15)


@dataclass(frozen=True)
class ScriptPromptTemplate:
    locale: str
    version: str

    @property
    def template_name(self) -> str:
        """``load_prompt_template`` に渡す名前（``prompts/script/<locale>.md``）。"""
        return f"script/{self.locale}"

    @property
    def template_id(self) -> str:
        """Artifact の input_hash に入る id。旧 ``script_ja``（ADR-0012 期）とは別物。"""
        return f"script.{self.locale}"


SCRIPT_PROMPT_TEMPLATES: dict[str, ScriptPromptTemplate] = {
    t.locale: t
    for t in (
        ScriptPromptTemplate(locale="ja-JP", version="1"),
        ScriptPromptTemplate(locale="en-US", version="2"),
    )
}


def script_prompt_template(locale: str) -> ScriptPromptTemplate:
    try:
        return SCRIPT_PROMPT_TEMPLATES[locale]
    except KeyError:
        raise ValueError(f"no script prompt template for locale {locale!r}") from None


def render_localized_script_prompt(
    *,
    locale: str,
    language: str,
    subject_matter_json: str,
    format_brief: str,
    duration_min_seconds: int,
    duration_max_seconds: int,
    schema_json: str,
    max_speech_units_per_second: float,
    narration_budget_table: str,
) -> str:
    """locale のテンプレートに、題材・形式・スキーマを**データとして**埋める。"""
    values = {
        "language": language,
        "subject_matter_json": subject_matter_json,
        "format_brief": format_brief,
        "duration_min_seconds": str(duration_min_seconds),
        "duration_max_seconds": str(duration_max_seconds),
        "schema_json": schema_json,
        "max_speech_units_per_second": f"{max_speech_units_per_second:g}",
        "narration_budget_table": narration_budget_table,
    }
    return _render(script_prompt_template(locale).template_name, values)


def narration_budget_table(locale: ScriptLocale) -> str:
    """シーン尺ごとのナレーション上限（``ScriptLocale.narration_budget``）を prompt 用に並べる。

    単位名は英語（``words`` / ``characters``）。数値はすべて ``ScriptLocale`` から導く。
    """
    unit = f"{locale.speech_unit.value}s"
    return "\n".join(
        f"- {sec} s: at most {locale.narration_budget(sec * 1000)} {unit}"
        for sec in NARRATION_BUDGET_EXAMPLE_SECONDS
    )


__all__ = [
    "NARRATION_BUDGET_EXAMPLE_SECONDS",
    "narration_budget_table",
    "SCRIPT_PROMPT_TEMPLATES",
    "ScriptPromptTemplate",
    "render_localized_script_prompt",
    "script_prompt_template",
]
