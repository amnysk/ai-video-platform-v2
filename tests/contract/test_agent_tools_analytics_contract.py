"""Analytics tool の契約（ADR-0047 §5）。理由は docs/testing/agent-tools-rationale.md。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.agent_tool_registry import TOOL_DEFS
from contracts.agent_tools import ToolEffect, ToolId
from contracts.agent_tools_analytics import (
    ANALYTICS_TOOL_DEFS,
    CompareInput,
    VideoAnalyticsInput,
)

ANALYTICS_IDS = {
    ToolId.GET_VIDEO_ANALYTICS,
    ToolId.GET_AUDIENCE_RETENTION,
    ToolId.COMPARE_VIDEO_PERFORMANCE,
    ToolId.GET_AUDIENCE_DEMOGRAPHICS,
    ToolId.GET_CHANNEL_PERFORMANCE,
}


def test_five_analytics_tools_are_registered_read_only_with_youtube_quota() -> None:
    assert {d.tool_id for d in ANALYTICS_TOOL_DEFS} == ANALYTICS_IDS
    for d in ANALYTICS_TOOL_DEFS:
        assert d.version == 1
        assert d.effect is ToolEffect.READ
        assert d.cost_class == "youtube_quota"
        assert d.ref in TOOL_DEFS


def test_models_forbid_extra_fields_and_are_frozen() -> None:
    for d in ANALYTICS_TOOL_DEFS:
        for model in (d.input_model, d.output_model):
            assert model.model_config.get("extra") == "forbid", model
            assert model.model_config.get("frozen") is True, model


def test_video_id_must_look_like_a_video_id() -> None:
    with pytest.raises(ValidationError):
        VideoAnalyticsInput(video_id="not an id; drop")
    with pytest.raises(ValidationError):
        VideoAnalyticsInput.model_validate({"video_id": "abcdefghijk", "write": True})


def test_range_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        VideoAnalyticsInput.model_validate(
            {"video_id": "abcdefghijk", "start_date": "2026-09-10", "end_date": "2026-09-01"}
        )


def test_compare_needs_2_to_10_unique_videos() -> None:
    ids = [f"video{i:06d}" for i in range(11)]
    CompareInput(video_ids=tuple(ids[:2]))
    CompareInput(video_ids=tuple(ids[:10]))
    for bad in (ids[:1], ids[:11], [ids[0], ids[0]]):
        with pytest.raises(ValidationError):
            CompareInput(video_ids=tuple(bad))
