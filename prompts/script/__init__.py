"""台本 prompt の locale 別 registry（ADR-0026）。

テンプレートは locale（言語）だけで選ぶ。形式（Shorts / Long）は content profile の
**データ**（``format_brief`` と ``script_duration_seconds``）として埋める。
workflow / activity のコードにファイル名を書かない。ここの鍵は
``contracts.topic_planning.SCRIPT_LOCALES`` の鍵と一致する（テストが検査する）。
テンプレートを変えたら、その locale の ``version`` を上げる（``script_input_hash`` に入る）。
"""

from __future__ import annotations

from dataclasses import dataclass

from prompts import _render


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
        ScriptPromptTemplate(locale="en-US", version="1"),
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
) -> str:
    """locale のテンプレートに、題材・形式・スキーマを**データとして**埋める。"""
    values = {
        "language": language,
        "subject_matter_json": subject_matter_json,
        "format_brief": format_brief,
        "duration_min_seconds": str(duration_min_seconds),
        "duration_max_seconds": str(duration_max_seconds),
        "schema_json": schema_json,
    }
    return _render(script_prompt_template(locale).template_name, values)


__all__ = [
    "SCRIPT_PROMPT_TEMPLATES",
    "ScriptPromptTemplate",
    "render_localized_script_prompt",
    "script_prompt_template",
]
