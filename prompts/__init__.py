"""プロンプトテンプレートの読み込み。

**巨大なプロンプト本文を Python コードに直書きしない。** 本文は ``prompts/*.md`` に置き、
差分がレビューできる形にする。テンプレートを変えたら
``PROMPT_TEMPLATE_VERSION`` を上げる（生成物のメタデータに載せて追跡するため）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

PROMPT_TEMPLATE_ID = "script_ja"
PROMPT_TEMPLATE_VERSION = "1"
STORYBOARD_PROMPT_TEMPLATE_ID = "storyboard_ja"
STORYBOARD_PROMPT_TEMPLATE_VERSION = "1"
TOPIC_PROMPT_TEMPLATE_ID = "topic_en"
TOPIC_PROMPT_TEMPLATE_VERSION = "2"
#: topic_plans.prompt_version に記録する値
TOPIC_PROMPT_VERSION = f"{TOPIC_PROMPT_TEMPLATE_ID}@{TOPIC_PROMPT_TEMPLATE_VERSION}"

PROMPTS_DIR = Path(__file__).resolve().parent
_NAME_RE = re.compile(r"\A[a-z0-9_]+\Z")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")


def load_prompt_template(name: str) -> str:
    """``prompts/<name>.md`` を読む。``name`` はパス要素を含めない。"""
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid prompt template name: {name!r}")
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise ValueError(f"unknown prompt template: {name!r}")
    return path.read_text(encoding="utf-8")


def render_script_prompt(
    *,
    topic: str,
    language: str = "ja",
    schema_json: str,
    duration_hint: str = "30〜45秒",
    template_name: str = PROMPT_TEMPLATE_ID,
) -> str:
    """台本生成プロンプトを組み立てる。

    ``episode_id`` / ``type`` / ``schema_version`` / ``generator*`` は
    **呼び出し側が注入する**ので、ここでは渡さない。
    """
    values = {
        "topic": topic,
        "language": language,
        "schema_json": schema_json,
        "duration_hint": duration_hint,
    }
    return _render(template_name, values)


def render_storyboard_prompt(
    *,
    spec_markdown: str,
    output_schema_json: str,
    script_json: str,
    total_duration_seconds: str,
    language: str = "ja",
    template_name: str = STORYBOARD_PROMPT_TEMPLATE_ID,
) -> str:
    """storyboard 生成プロンプトを組み立てる。値は1回だけ置換する（値の中の ``{{}}`` は残る）。"""
    values = {
        "spec_markdown": spec_markdown,
        "output_schema_json": output_schema_json,
        "script_json": script_json,
        "total_duration_seconds": total_duration_seconds,
        "language": language,
    }
    return _render(template_name, values)


def render_topic_prompt(
    *,
    strategy_json: str,
    format_brief: str,
    analytics_summary_json: str,
    memory_subjects: Sequence[str],
    recent_topics: Sequence[str],
    avoid_subjects: Sequence[str],
    candidate_count_min: int,
    candidate_count_max: int,
    schema_json: str,
    template_name: str = TOPIC_PROMPT_TEMPLATE_ID,
) -> str:
    """Topic 候補の生成プロンプト（ADR-0025）。

    strategy・形式・Analytics・Content Memory は**すべてデータとして**受け取る。
    テンプレートに Shorts や特定チャンネルの値を書かない（profile を変えるだけで別の形式に使える）。
    """
    values = {
        "strategy_json": strategy_json,
        "format_brief": format_brief,
        "analytics_summary": analytics_summary_json,
        "memory_subjects": json.dumps(list(memory_subjects), ensure_ascii=False),
        "recent_topics": json.dumps(list(recent_topics), ensure_ascii=False),
        "avoid_subjects": json.dumps(list(avoid_subjects), ensure_ascii=False),
        "candidate_count_min": str(candidate_count_min),
        "candidate_count_max": str(candidate_count_max),
        "schema_json": schema_json,
    }
    return _render(template_name, values)


def _render(template_name: str, values: dict[str, str]) -> str:
    template = load_prompt_template(template_name)
    missing = {m.group(1) for m in _PLACEHOLDER_RE.finditer(template)} - set(values)
    if missing:
        raise ValueError(f"template {template_name!r} has unfilled placeholders: {sorted(missing)}")
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)
