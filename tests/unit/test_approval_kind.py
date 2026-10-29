"""承認の種別（所有者本人 / エージェントへの委任）の検証（ADR-0080 / INV-123）。"""

from __future__ import annotations

import pytest

from contracts.approval import ApproverKind
from domain.approval import (
    OWNER_ONLY_GATES,
    ApprovalError,
    OwnerGate,
    format_approver,
    parse_approval_reference,
    parse_approver,
    require_gate_allowed,
)

OWNER = "OWNER-20261029-chatmsg01"
DELEGATED = "AGENT-20261029-deploy01:OWNER-20261029-instr01"


def test_an_owner_reference_is_owner_kind() -> None:
    ref = parse_approval_reference(OWNER)
    assert ref.kind is ApproverKind.OWNER
    assert ref.delegated_by is None


def test_a_delegated_reference_names_the_owner_instruction_it_relies_on() -> None:
    ref = parse_approval_reference(DELEGATED)
    assert ref.kind is ApproverKind.AGENT_DELEGATED
    assert ref.delegated_by == "OWNER-20261029-instr01"


@pytest.mark.parametrize(
    "token",
    [
        "",
        " ",
        "approved",
        "owner",
        "OWNER-20261329-abcdef",  # 実在しない日付
        "OWNER-20261029-ab",  # id が短い
        "owner-20261029-abcdef",  # 小文字
        "AGENT-20261029-deploy01",  # 委任元の指示が無い
        "AGENT-20261029-deploy01:",
        "AGENT-20261029-deploy01:approved",
        "AGENT-20261029-deploy01:AGENT-20261029-other01",  # 委任元は所有者の指示であること
        "AGENT-20261029-deploy01:OWNER-20261029-instr01:OWNER-20261029-more01",
        "OWNER-20261029-abcdef:OWNER-20261029-instr01",  # 所有者承認に委任元は付かない
        "AGENT-20261329-deploy01:OWNER-20261029-instr01",
        "AGENT-20261029-deploy01:OWNER-20261329-instr01",
        OWNER + "\n",
    ],
)
def test_anything_that_does_not_state_a_valid_kind_is_not_an_approval(token: str) -> None:
    with pytest.raises(ApprovalError):
        parse_approval_reference(token)


def test_the_owner_only_gates_are_the_ones_that_spend_or_publish() -> None:
    assert {g.value for g in OWNER_ONLY_GATES} == {
        "enable_new_feature",
        "raise_provider_budget",
        "public_or_unlisted_upload",
        "release_provider_credit_hold",
        "resolve_reservation_manually",
    }
    assert set(OwnerGate) == OWNER_ONLY_GATES  # 列挙された関門はすべて所有者専用


@pytest.mark.parametrize("gate", sorted(OwnerGate))
def test_an_agent_delegated_approval_never_satisfies_an_owner_gate(gate: OwnerGate) -> None:
    require_gate_allowed(parse_approval_reference(OWNER), gate)
    with pytest.raises(ApprovalError, match="owner"):
        require_gate_allowed(parse_approval_reference(DELEGATED), gate)


def test_a_missing_reference_never_satisfies_an_owner_gate() -> None:
    with pytest.raises(ApprovalError):
        require_gate_allowed(None, OwnerGate.ENABLE_NEW_FEATURE)


def test_the_stored_actor_states_its_kind_and_a_legacy_value_does_not() -> None:
    stored = format_approver(ApproverKind.OWNER, "yoshiki")
    assert stored == "owner:yoshiki"
    assert parse_approver(stored) == (ApproverKind.OWNER, "yoshiki")
    assert parse_approver("agent_delegated:sonnet") == (ApproverKind.AGENT_DELEGATED, "sonnet")
    # 種別を書かない旧記録は所有者承認ではない
    assert parse_approver("yoshiki") == (None, "yoshiki")
    assert parse_approver("owner") == (None, "owner")
    assert parse_approver(None) == (None, "")
    # 名前に種別を紛れ込ませても、先頭の種別が優先される（後ろの ':' は名前）
    assert parse_approver("agent_delegated:owner:x") == (ApproverKind.AGENT_DELEGATED, "owner:x")


def test_an_actor_name_cannot_forge_a_kind() -> None:
    with pytest.raises(ApprovalError):
        format_approver(ApproverKind.AGENT_DELEGATED, "owner:yoshiki")
    with pytest.raises(ApprovalError):
        format_approver(ApproverKind.OWNER, "")
    with pytest.raises(ApprovalError):
        format_approver(ApproverKind.OWNER, "x" * 80)
