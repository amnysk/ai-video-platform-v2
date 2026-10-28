"""INV-103: Agent の tool allowlist は read-only のみ（design v3 T04）。"""

from __future__ import annotations

import pytest

from contracts.agent_tools import (
    ALLOWED_TOOL_IDS,
    FORBIDDEN_TOOL_IDS,
    AllowedTool,
    ToolRejection,
)
from domain.quality import validate_tool_request


def test_allowlist_is_exactly_the_six_read_only_tools() -> None:
    assert {
        "research.search",
        "research.fetch",
        "geo.resolve",
        "geo.verify",
        "bgm.list_approved",
        "artifact.read",
    } == ALLOWED_TOOL_IDS


def test_forbidden_tools_never_overlap_the_allowlist() -> None:
    assert not (ALLOWED_TOOL_IDS & FORBIDDEN_TOOL_IDS)


@pytest.mark.parametrize("tool", list(AllowedTool))
def test_allowed_tools_pass(tool: AllowedTool) -> None:
    d = validate_tool_request(tool.value)
    assert d.allowed and d.rejection is None


@pytest.mark.parametrize("tool", sorted(FORBIDDEN_TOOL_IDS))
def test_forbidden_tools_are_rejected_as_forbidden(tool: str) -> None:
    d = validate_tool_request(tool)
    assert not d.allowed and d.rejection is ToolRejection.FORBIDDEN and d.tool_id == tool


def test_a_web_document_injected_upload_request_is_rejected_and_structured() -> None:
    """T04: 資料に「upload しろ」と書かれていても、tool 要求は allowlist だけで決まる。"""
    d = validate_tool_request("youtube.upload")
    assert not d.allowed
    assert d.rejection is ToolRejection.FORBIDDEN
    assert d.detail  # 記録に載せる固定形の理由


@pytest.mark.parametrize(
    "tool", ["Research.Search", " research.search", "research.search ", "upload", "shell", "*"]
)
def test_lookalike_or_unknown_tools_are_rejected(tool: str) -> None:
    d = validate_tool_request(tool)
    assert not d.allowed and d.rejection is ToolRejection.NOT_ALLOWLISTED


@pytest.mark.parametrize("tool", [None, 3, "", "  ", ["research.search"], b"geo.verify"])
def test_malformed_requests_are_rejected(tool) -> None:
    d = validate_tool_request(tool)
    assert not d.allowed and d.rejection is ToolRejection.MALFORMED


def test_a_long_unknown_id_is_truncated_in_the_record() -> None:
    assert len(validate_tool_request("x" * 5000).tool_id) <= 64
