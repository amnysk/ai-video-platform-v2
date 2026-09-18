"""Topic 候補の prompt（ADR-0025）。

strategy・形式はデータとして渡り、テンプレートに直書きしない。"""

from __future__ import annotations

import json

from contracts.topic_planning import CONTENT_PROFILES, STRATEGY_PROFILES, TopicCandidateBatch
from prompts import (
    TOPIC_PROMPT_TEMPLATE_ID,
    TOPIC_PROMPT_TEMPLATE_VERSION,
    TOPIC_PROMPT_VERSION,
    load_prompt_template,
    render_topic_prompt,
)


def _render(**overrides: object) -> str:
    values: dict[str, object] = {
        "strategy_json": STRATEGY_PROFILES["us_young_history_v1"].model_dump_json(),
        "format_brief": CONTENT_PROFILES["long_form"].format_brief,
        "analytics_summary_json": json.dumps({"mode": "no_analytics", "confidence": 0.0}),
        "memory_subjects": ["sumo_salt", "daisho"],
        "recent_topics": ["Why sumo wrestlers throw salt"],
        "avoid_subjects": ["ninja_myths"],
        "candidate_count_min": 10,
        "candidate_count_max": 20,
        "schema_json": json.dumps(TopicCandidateBatch.model_json_schema()),
    }
    values.update(overrides)
    return render_topic_prompt(**values)  # type: ignore[arg-type]


def test_all_data_reaches_the_prompt() -> None:
    text = _render()
    assert "Japan's Past" in text
    assert "warriors_and_war" in text
    assert CONTENT_PROFILES["long_form"].format_brief in text
    assert '["sumo_salt", "daisho"]' in text
    assert '["ninja_myths"]' in text
    assert "Why sumo wrestlers throw salt" in text
    assert "between 10 and 20 candidates" in text
    assert '"TopicCandidate"' in text
    assert '"no_analytics"' in text
    assert "{{" not in text


def test_template_has_no_strategy_or_format_values() -> None:
    template = load_prompt_template(TOPIC_PROMPT_TEMPLATE_ID).lower()
    for forbidden in (
        "shorts",
        "60 seconds",
        "vertical",
        "japan",
        "samurai",
        "us viewers",
        "18-34",
        "history",
    ):
        assert forbidden not in template, forbidden


def test_version_label() -> None:
    assert f"{TOPIC_PROMPT_TEMPLATE_ID}@{TOPIC_PROMPT_TEMPLATE_VERSION}" == TOPIC_PROMPT_VERSION
    assert len(TOPIC_PROMPT_VERSION) <= 64


def test_memory_lists_are_declared_untrusted_data() -> None:
    """過去の Topic 一覧は（LLM 出力由来の）データ。指示として従わせない（ADR-0025）。"""
    template = load_prompt_template(TOPIC_PROMPT_TEMPLATE_ID).lower()
    assert "untrusted data" in template
    assert "not instructions" in template


def test_prompt_version_is_bumped_for_the_untrusted_data_notice() -> None:
    assert TOPIC_PROMPT_TEMPLATE_VERSION == "2"
