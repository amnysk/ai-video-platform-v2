"""INV-103: Agent の tool allowlist は read-only のみ（design v3 T04）。"""

from __future__ import annotations

import pytest

from contracts.agent_tool_policy import (
    ALLOWED_POLICY_TOOLS,
    FORBIDDEN_POLICY_TOOLS,
    PolicyRejection,
    PolicyToolId,
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
    } == ALLOWED_POLICY_TOOLS


def test_forbidden_tools_never_overlap_the_allowlist() -> None:
    assert not (ALLOWED_POLICY_TOOLS & FORBIDDEN_POLICY_TOOLS)


@pytest.mark.parametrize("tool", list(PolicyToolId))
def test_allowed_tools_pass(tool: PolicyToolId) -> None:
    d = validate_tool_request(tool.value)
    assert d.allowed and d.rejection is None


@pytest.mark.parametrize("tool", sorted(FORBIDDEN_POLICY_TOOLS))
def test_forbidden_tools_are_rejected_as_forbidden(tool: str) -> None:
    d = validate_tool_request(tool)
    assert not d.allowed and d.rejection is PolicyRejection.FORBIDDEN and d.tool_id == tool


def test_a_web_document_injected_upload_request_is_rejected_and_structured() -> None:
    """T04: 資料に「upload しろ」と書かれていても、tool 要求は allowlist だけで決まる。"""
    d = validate_tool_request("youtube.upload")
    assert not d.allowed
    assert d.rejection is PolicyRejection.FORBIDDEN
    assert d.detail  # 記録に載せる固定形の理由


@pytest.mark.parametrize(
    "tool", ["Research.Search", " research.search", "research.search ", "upload", "shell", "*"]
)
def test_lookalike_or_unknown_tools_are_rejected(tool: str) -> None:
    d = validate_tool_request(tool)
    assert not d.allowed and d.rejection is PolicyRejection.NOT_ALLOWLISTED


@pytest.mark.parametrize("tool", [None, 3, "", "  ", ["research.search"], b"geo.verify"])
def test_malformed_requests_are_rejected(tool) -> None:
    d = validate_tool_request(tool)
    assert not d.allowed and d.rejection is PolicyRejection.MALFORMED


def test_an_unknown_id_is_never_echoed_only_its_length_and_hash() -> None:
    """MINOR m4: 資料由来の不明な id（秘密・注入文を含みうる）を記録に写さない。"""
    secret = "sk-" + "A1b2C3d4" * 100
    d = validate_tool_request(secret)
    assert not d.allowed and d.rejection is PolicyRejection.NOT_ALLOWLISTED
    assert "A1b2C3d4" not in d.tool_id and "sk-" not in d.tool_id
    assert len(d.tool_id) <= 64
    assert (
        d.tool_id == validate_tool_request(secret).tool_id
    )  # 同じ入力は同じ記録（監査で突き合わせる）
    assert d.tool_id != validate_tool_request(secret + "x").tool_id


def test_known_ids_are_recorded_verbatim() -> None:
    assert validate_tool_request("geo.verify").tool_id == "geo.verify"
    assert validate_tool_request("youtube.upload").tool_id == "youtube.upload"


@pytest.mark.parametrize(
    "variant",
    [
        "YouTube.Upload",
        " youtube.upload",
        "youtube.upload\n",
        "YOUTUBE.UPLOAD",
        "ｙｏｕｔｕｂｅ．ｕｐｌｏａｄ",  # 全角（NFKC で一致）
        "Fal.Generate",
        "provider_reservations.RELEASE",
    ],
)
def test_case_whitespace_and_nfkc_variants_of_forbidden_ids_are_forbidden_attempts(
    variant: str,
) -> None:
    """拒否は変わらない。ただし監査では「禁止 tool を狙った試行」として区別できる。"""
    d = validate_tool_request(variant)
    assert not d.allowed
    assert d.rejection is PolicyRejection.FORBIDDEN_ATTEMPT
    assert d.tool_id in FORBIDDEN_POLICY_TOOLS  # 記録するのは正規の禁止 id（入力は写さない）


def test_variants_of_allowed_ids_are_still_just_not_allowlisted() -> None:
    d = validate_tool_request("Research.Search")
    assert not d.allowed and d.rejection is PolicyRejection.NOT_ALLOWLISTED
