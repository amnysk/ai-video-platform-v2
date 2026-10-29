"""release 承認記録は承認の種別を述べ、委任された承認は所有者専用の関門を通さない（ADR-0080）。

2026-10-29 の Phase 2 デプロイでは、所有者の条件付き指示を根拠にエージェントが承認ファイルを書いた
（v3-phase2-report §3.4）。記録からは所有者本人の承認と区別できなかった。それを防ぐ。
"""

from __future__ import annotations

import pytest

from domain import release_preflight as rp
from tests.unit.test_release_preflight import Fake, _approval, _run, _status

DELEGATED = "AGENT-20261029-deploy01:OWNER-20261029-instr01"


def _result(extra: dict[str, object]) -> rp.CheckResult:
    fake = Fake()
    fake.texts = {**fake.texts, "approval.json": _approval(extra=extra)}
    report = _run(fake)
    return next(r for r in report.results if r.name == "owner_approval")


def test_an_owner_approval_passes_and_says_so() -> None:
    result = _result({})
    assert result.status is rp.Status.PASS
    assert result.data["approver_kind"] == "owner"


def test_a_delegated_approval_passes_a_flags_off_deploy_and_is_recorded_as_delegated() -> None:
    result = _result({"approval_ref": DELEGATED})
    assert result.status is rp.Status.PASS
    assert result.data["approver_kind"] == "agent_delegated"
    assert result.data["delegated_by"] == "OWNER-20261029-instr01"
    assert "agent_delegated" in result.detail


@pytest.mark.parametrize("ref", [None, "", "owner", "approved", "OWNER-20261329-abcdef", 7])
def test_a_record_that_does_not_state_its_kind_is_not_an_approval(ref: object) -> None:
    result = _result({"approval_ref": ref})
    assert result.status is rp.Status.UNKNOWN


def test_a_record_without_any_approval_ref_key_is_unknown() -> None:
    fake = Fake()
    import json

    doc = json.loads(_approval())
    del doc["approval_ref"]
    fake.texts = {**fake.texts, "approval.json": json.dumps(doc)}
    report = _run(fake)
    assert _status(report, "owner_approval") is rp.Status.UNKNOWN


def test_an_explicit_kind_that_contradicts_the_reference_is_rejected() -> None:
    assert _result({"approver_kind": "owner", "approval_ref": DELEGATED}).status is (
        rp.Status.UNKNOWN
    )
    assert _result({"approver_kind": "agent_delegated"}).status is rp.Status.UNKNOWN
    assert _result({"approver_kind": "root"}).status is rp.Status.UNKNOWN


@pytest.mark.parametrize(
    "gate",
    [
        "enable_new_feature",
        "raise_fal_budget",
        "public_or_unlisted_upload",
        "release_provider_credit_hold",
        "resolve_reservation_manually",
    ],
)
def test_a_delegated_approval_cannot_enable_an_owner_gate(gate: str) -> None:
    blocked = _result({"approval_ref": DELEGATED, "enables": [gate]})
    assert blocked.status is rp.Status.BLOCKED
    assert gate in blocked.detail
    assert _result({"enables": [gate]}).status is rp.Status.PASS  # 所有者本人なら通る


def test_an_unknown_gate_name_in_enables_fails_closed() -> None:
    assert _result({"enables": ["turn_everything_on"]}).status is rp.Status.UNKNOWN
    assert _result({"enables": "enable_new_feature"}).status is rp.Status.UNKNOWN
    assert _result({"approval_ref": DELEGATED, "enables": ["nope"]}).status is rp.Status.UNKNOWN


# ---- 同じ規則を使う他の承認経路（ADR-0080）


def test_the_release_reservation_waiver_stays_owner_only() -> None:
    from tests.unit.test_release_reservation_waiver import _check, _fake, _waiver

    ok = _check(_fake())
    assert ok.status is rp.Status.WAIVED
    delegated = _check(_fake(_waiver(owner_approval=DELEGATED)))
    assert delegated.status is rp.Status.BLOCKED


def test_provider_credit_actions_accept_only_the_owners_own_reference() -> None:
    from infrastructure.production.provider_credits import CreditsRefused, validate_approval

    ok = validate_approval(owner_approval="OWNER-20261029-chatmsg01", evidence="e", actor="yoshiki")
    assert ok.actor == "owner:yoshiki"  # 種別つきで approved_by 列に入る
    with pytest.raises(CreditsRefused, match="owner"):
        validate_approval(owner_approval=DELEGATED, evidence="e", actor="sonnet")
    with pytest.raises(CreditsRefused):
        validate_approval(owner_approval="OWNER-20261029-chatmsg01", evidence="e", actor="owner:x")


def test_manual_reservation_resolution_accepts_only_the_owners_own_reference() -> None:
    from domain.provider.reconciliation import ManualResolutionError, validate_manual_resolution

    ok = validate_manual_resolution(
        outcome="success",
        evidence="e",
        owner_approval="OWNER-20261029-chatmsg01",
        actor="yoshiki",
    )
    assert ok.actor == "owner:yoshiki"
    assert ok.reconciled_by == "operator:yoshiki"  # 既存の形は変えない
    with pytest.raises(ManualResolutionError, match="agent-delegated"):
        validate_manual_resolution(
            outcome="success", evidence="e", owner_approval=DELEGATED, actor="sonnet"
        )
