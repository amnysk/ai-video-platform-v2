"""Topic 候補 prompt への Trend の節（ADR-0039 §B6。opt-in・既定 OFF）。

**Trend が無いときの prompt はこのモジュールを通らない**: ``render_topic_prompt``
（``prompts/__init__.py``）と ``prompts/topic_en.md`` は接続前（f209e7c）のまま変えていないので、
Trend を使わない prompt と ``TOPIC_PROMPT_VERSION``（``topic_en@2``）はバイト単位で同じになる。

Trend を使うときだけ、描画済みの prompt の ``# Requirements for each candidate`` の見出しの直前に
``prompts/topic_trend_en.md`` の節を差し込み、記録する版を ``TOPIC_TREND_PROMPT_VERSION``
（``topic_en@2+topic_trend_en@1``）にする。節の本文を変えたら
``TOPIC_TREND_PROMPT_TEMPLATE_VERSION`` を上げる（``topic_en.md`` の版とは独立）。
"""

from __future__ import annotations

import re

from prompts import TOPIC_PROMPT_VERSION, load_prompt_template

__all__ = [
    "TOPIC_TREND_PROMPT_TEMPLATE_ID",
    "TOPIC_TREND_PROMPT_TEMPLATE_VERSION",
    "TOPIC_TREND_PROMPT_VERSION",
    "TREND_SECTION_ANCHOR",
    "add_trend_section",
]

TOPIC_TREND_PROMPT_TEMPLATE_ID = "topic_trend_en"
TOPIC_TREND_PROMPT_TEMPLATE_VERSION = "1"
#: Trend の節を含む prompt のときに topic_plans.prompt_version へ記録する値
TOPIC_TREND_PROMPT_VERSION = (
    f"{TOPIC_PROMPT_VERSION}+{TOPIC_TREND_PROMPT_TEMPLATE_ID}@{TOPIC_TREND_PROMPT_TEMPLATE_VERSION}"
)
#: 節を差し込む位置（``topic_en.md`` の見出し行）。行全体で一致させる: データ（JSON に符号化した
#: 一覧）は改行を含まないので、見出し行と取り違えない
TREND_SECTION_ANCHOR = "\n# Requirements for each candidate\n"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")


def add_trend_section(prompt: str, trend_summary_json: str) -> str | None:
    """Trend の節を差し込んだ prompt。

    見出しがちょうど 1 つでなければ ``None``（Trend 無しで続ける）。
    """
    if prompt.count(TREND_SECTION_ANCHOR) != 1:
        return None
    template = load_prompt_template(TOPIC_TREND_PROMPT_TEMPLATE_ID)
    placeholders = {m.group(1) for m in _PLACEHOLDER_RE.finditer(template)}
    if placeholders != {"trend_summary"}:
        raise ValueError(
            f"template {TOPIC_TREND_PROMPT_TEMPLATE_ID!r} placeholders: {sorted(placeholders)}"
        )
    # 値は 1 回だけ置換する（値の中の ``{{}}`` は残る。``prompts._render`` と同じ）
    section = _PLACEHOLDER_RE.sub(lambda _m: trend_summary_json, template)
    head, anchor, tail = prompt.partition(TREND_SECTION_ANCHOR)
    # 見出しの前の空行を保ったまま、節（末尾に空行を持つ）を見出しの直前に置く
    return head + "\n" + section + anchor.removeprefix("\n") + tail
