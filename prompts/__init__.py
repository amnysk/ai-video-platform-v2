"""プロンプトテンプレートの読み込み。

**巨大なプロンプト本文を Python コードに直書きしない。** 本文は ``prompts/*.md`` に置き、
差分がレビューできる形にする。テンプレートを変えたら
``PROMPT_TEMPLATE_VERSION`` を上げる（生成物のメタデータに載せて追跡するため）。
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPT_TEMPLATE_ID = "script_ja"
PROMPT_TEMPLATE_VERSION = "1"

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
    template = load_prompt_template(template_name)
    missing = {m.group(1) for m in _PLACEHOLDER_RE.finditer(template)} - set(values)
    if missing:
        raise ValueError(f"template {template_name!r} has unfilled placeholders: {sorted(missing)}")
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)
