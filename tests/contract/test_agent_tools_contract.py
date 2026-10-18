"""Agent Tool の契約（ADR-0047 §1、INV-58/61/62）。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from contracts.agent_tools import (
    TOOL_NAMESPACE,
    ToolDef,
    ToolEffect,
    ToolEnvelope,
    ToolId,
    ToolStatus,
    parse_tool_ref,
    tool_call_id,
    tool_ref,
)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    q: str


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    n: int


def _def(**kw: object) -> ToolDef:
    base: dict[str, object] = {
        "tool_id": ToolId.SEARCH_TRENDS,
        "version": 1,
        "description": "d",
        "input_model": _In,
        "output_model": _Out,
        "timeout_seconds": 10,
        "max_calls_per_run": 2,
        "cost_class": "none",
    }
    base.update(kw)
    return ToolDef(**base)  # type: ignore[arg-type]


def test_the_eight_tool_ids_are_the_requested_vocabulary() -> None:
    assert {t.value for t in ToolId} == {
        "search_trends",
        "search_historical_facts",
        "fetch_sources",
        "get_video_analytics",
        "get_audience_retention",
        "compare_video_performance",
        "get_audience_demographics",
        "get_channel_performance",
    }


def test_tool_refs_round_trip_and_reject_garbage() -> None:
    assert tool_ref(ToolId.FETCH_SOURCES, 2) == "fetch_sources@2"
    assert parse_tool_ref("fetch_sources@2") == (ToolId.FETCH_SOURCES, 2)
    for bad in ("fetch_sources", "fetch_sources@x", "nope@1", "fetch_sources@0", "@1"):
        with pytest.raises(ValueError):
            parse_tool_ref(bad)


def test_only_read_tools_can_be_defined() -> None:
    """INV-61: 投稿・削除・更新の tool は契約の段階で作れない。"""
    assert {e.value for e in ToolEffect} == {"read"}


def test_a_tool_def_rejects_nonpositive_limits() -> None:
    for kw in ({"timeout_seconds": 0}, {"max_calls_per_run": 0}, {"version": 0}):
        with pytest.raises(ValueError):
            _def(**kw)


def test_json_schema_is_derived_from_the_models_not_written_by_hand() -> None:
    d = _def()
    assert d.input_schema() == _In.model_json_schema()
    assert d.output_schema() == _Out.model_json_schema()
    assert d.ref == "search_trends@1"


def test_tool_call_id_is_deterministic_and_depends_on_every_part() -> None:
    a = tool_call_id("run-1", 0, ToolId.SEARCH_TRENDS, "a" * 64)
    assert a == tool_call_id("run-1", 0, ToolId.SEARCH_TRENDS, "a" * 64)
    assert str(__import__("uuid").UUID(a)) == a
    others = {
        tool_call_id("run-2", 0, ToolId.SEARCH_TRENDS, "a" * 64),
        tool_call_id("run-1", 1, ToolId.SEARCH_TRENDS, "a" * 64),
        tool_call_id("run-1", 0, ToolId.FETCH_SOURCES, "a" * 64),
        tool_call_id("run-1", 0, ToolId.SEARCH_TRENDS, "b" * 64),
    }
    assert a not in others and len(others) == 4
    assert TOOL_NAMESPACE is not None


def test_status_vocabulary_distinguishes_the_four_failure_kinds() -> None:
    """INV-58: API 非対応・データ不足・権限不足・反映遅延を区別して返す。"""
    values = {s.value for s in ToolStatus}
    assert {
        "not_available_via_api",
        "insufficient_data",
        "no_data",
        "withheld_by_threshold",
        "permission_denied",
        "reporting_delay",
        "quota_exceeded",
        "pending",
        "provider_not_configured",
    } <= values


def test_envelope_data_is_present_only_for_ok_and_always_untrusted() -> None:
    ok = ToolEnvelope(
        tool_call_id="c",
        tool_id=ToolId.SEARCH_TRENDS,
        tool_version=1,
        status=ToolStatus.OK,
        data={"n": 1},
    )
    assert ok.untrusted is True
    with pytest.raises(ValueError):
        ToolEnvelope(
            tool_call_id="c",
            tool_id=ToolId.SEARCH_TRENDS,
            tool_version=1,
            status=ToolStatus.NO_DATA,
            data={"n": 1},
        )
    with pytest.raises(ValueError):
        ToolEnvelope(
            tool_call_id="c",
            tool_id=ToolId.SEARCH_TRENDS,
            tool_version=1,
            status=ToolStatus.OK,
            data=None,
        )
    with pytest.raises(ValueError):
        ToolEnvelope(
            tool_call_id="c",
            tool_id=ToolId.SEARCH_TRENDS,
            tool_version=1,
            status=ToolStatus.OK,
            data={"n": 1},
            untrusted=False,  # type: ignore[arg-type]
        )


def test_the_registry_rejects_duplicate_registration() -> None:
    from contracts.agent_tool_registry import UnknownToolError, _index, resolve_tool

    d = _def()
    with pytest.raises(ValueError):
        _index((d, d))
    with pytest.raises(UnknownToolError):
        resolve_tool("search_trends@1", defs={})
    assert resolve_tool("search_trends@1", defs=_index((d,))) is d
