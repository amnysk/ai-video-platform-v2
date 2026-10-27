"""release 限定の未照合予約 waiver（ADR-0062 / INV-85）。collector は全て fake。

各テストが存在する理由は docs/testing/release-reservation-waiver-rationale.md。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

import pytest

from domain import release_preflight as rp
from tests.unit.test_release_preflight import (
    NOW,
    SHA,
    Fake,
    _approval,
    _request,
    _reviewed_report,
    sha256_text,
)

RID = "11111111-1111-1111-1111-111111111111"
REQ_ID = "fal-req-abc"
TOKEN = "OWNER-20261026-waiver1"
EVIDENCE_PATH = "evidence/recon.txt"
EVIDENCE_TEXT = "fal dashboard shows no charge for the request\n"
WAIVER_PATH = "waiver.json"


def _view(**kw: Any) -> rp.ReservationView:
    base: dict[str, Any] = {
        "id": RID,
        "provider": "fal_image",
        "status": "reserved",
        "provider_request_id": REQ_ID,
        "estimated_cost_usd": "0.0400",
        "reserved_at": "2026-10-19T00:00:00+00:00",
        "dispatched_at": "2026-10-19T00:00:05+00:00",
        "raw_output_key": None,
        "outcome_artifact_id": None,
    }
    base.update(kw)
    return rp.ReservationView(**base)


def _waiver(**kw: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "kind": "release_reservation_waiver",
        "sha": SHA,
        "reservation_id": RID,
        "provider_request_id": REQ_ID,
        "reservation_fingerprint": rp.reservation_fingerprint(_view()),
        "approver": "owner",
        "owner_approval": TOKEN,
        "approved_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(hours=24)).isoformat(),
        "reason": "request was dispatched while credits were exhausted; no charge expected",
        "required_budget_usd": "0",
        "evidence": [{"path": EVIDENCE_PATH, "sha256": sha256_text(EVIDENCE_TEXT)}],
    }
    doc.update(kw)
    return doc


@dataclass
class WFake(Fake):
    rows: Any = field(default_factory=lambda: [_view()])
    budget: Any = "0"
    running: Any = 0

    def __post_init__(self) -> None:
        self.pending = len(self.rows) if isinstance(self.rows, list) else 1
        self.texts = {**self.texts, EVIDENCE_PATH: EVIDENCE_TEXT}

    def pending_reservations(self) -> list[rp.ReservationView]:
        return self._get(self.rows)

    def paid_budget_setting(self) -> str | None:
        return self._get(self.budget)


def _fake(waiver: Any = "default", **kw: Any) -> WFake:
    f = WFake(**kw)
    if waiver == "default":
        waiver = _waiver()
    if waiver is not None:
        f.texts[WAIVER_PATH] = waiver if isinstance(waiver, str) else json.dumps(waiver)
    return f


def _check(f: WFake, **kw: Any) -> rp.CheckResult:
    report = rp.run_preflight(_request(reservation_waiver=WAIVER_PATH, **kw), f, NOW)
    return next(r for r in report.results if r.name == rp.CHECK_RESERVATIONS)


def _report(f: WFake, **kw: Any) -> rp.Report:
    return rp.run_preflight(_request(reservation_waiver=WAIVER_PATH, **kw), f, NOW)


# ---------------------------------------------------------------- 成立


def test_single_target_reservation_with_all_conditions_is_waived_and_ready() -> None:
    f = _fake()
    report = _report(f)
    r = next(x for x in report.results if x.name == rp.CHECK_RESERVATIONS)
    assert r.status is rp.Status.WAIVED, r.detail
    assert report.overall is rp.Overall.READY
    assert r.data["waived"] is True
    assert r.data["reservation_id"] == RID
    assert r.data["owner_approval_fingerprint"] == sha256_text(TOKEN)[:16]
    assert TOKEN not in json.dumps(dict(r.data)) + r.detail
    assert "remains reserved" in r.detail and SHA[:12] in r.detail


@pytest.mark.parametrize("budget", ["0", "0.00"])
def test_explicit_zero_budget_is_accepted(budget: str) -> None:
    assert _check(_fake(budget=budget)).status is rp.Status.WAIVED


def test_count_zero_ignores_the_waiver() -> None:
    f = _fake(rows=[])
    f.pending = 0
    r = _check(f)
    assert r.status is rp.Status.PASS and r.detail.startswith("no pending reservation")


def test_no_waiver_file_keeps_the_original_blocked_detail() -> None:
    r = _check(_fake(waiver=None))
    plain = rp.run_preflight(_request(), Fake(pending=1), NOW)
    original = next(x for x in plain.results if x.name == rp.CHECK_RESERVATIONS)
    assert r.status is rp.Status.BLOCKED
    assert r.detail.split(" [")[0] == original.detail.split(" [")[0]
    assert "1 unreconciled provider reservation(s)" in r.detail


# ---------------------------------------------------------------- 拒否


def _blocked(f: WFake, **kw: Any) -> rp.CheckResult:
    r = _check(f, **kw)
    assert r.status is rp.Status.BLOCKED, r.detail
    return r


def test_a_second_reserved_row_blocks() -> None:
    other = _view(id="22222222-2222-2222-2222-222222222222", provider_request_id="other")
    _blocked(_fake(rows=[_view(), other]))


def test_count_changing_during_the_read_blocks() -> None:
    f = _fake()
    f.pending = 2
    assert "count changed" in _blocked(f).detail


def test_expired_waiver_blocks() -> None:
    _blocked(_fake(_waiver(expires_at=(NOW - timedelta(minutes=1)).isoformat())))


def test_window_over_72_hours_blocks() -> None:
    at = NOW - timedelta(hours=1)
    _blocked(_fake(_waiver(expires_at=(at + timedelta(hours=73)).isoformat())))


def test_expiry_not_after_approval_blocks() -> None:
    at = NOW - timedelta(hours=1)
    _blocked(_fake(_waiver(approved_at=at.isoformat(), expires_at=at.isoformat())))


def test_approved_in_the_future_blocks() -> None:
    at = NOW + timedelta(hours=1)
    _blocked(
        _fake(_waiver(approved_at=at.isoformat(), expires_at=(at + timedelta(hours=2)).isoformat()))
    )


def test_naive_times_block() -> None:
    _blocked(_fake(_waiver(approved_at="2026-10-20T10:00:00", expires_at="2026-10-21T10:00:00")))


def test_request_id_mismatch_blocks() -> None:
    _blocked(_fake(_waiver(provider_request_id="someone-else")))


def test_reservation_id_mismatch_blocks() -> None:
    _blocked(_fake(_waiver(reservation_id="33333333-3333-3333-3333-333333333333")))


def test_wrong_sha_and_wrong_kind_block() -> None:
    _blocked(_fake(_waiver(sha="c" * 40)))
    _blocked(_fake(_waiver(kind="something_else")))


def test_unparseable_waiver_is_blocked_not_unknown() -> None:
    _blocked(_fake("{not json"))


def test_fingerprint_mismatch_blocks_when_the_row_changed() -> None:
    f = _fake(rows=[_view(raw_output_key="provider-raw/x")])
    assert "fingerprint" in _blocked(f).detail


def test_row_that_is_not_reserved_blocks() -> None:
    _blocked(_fake(rows=[_view(status="dispatched")]))


@pytest.mark.parametrize("budget", [None, "", "5", "-1", "abc", "NaN"])
def test_only_an_explicit_zero_budget_is_accepted(budget: str | None) -> None:
    _blocked(_fake(budget=budget))


def test_waiver_must_require_zero_budget() -> None:
    _blocked(_fake(_waiver(required_budget_usd="5")))


@pytest.mark.parametrize(
    "override", [{"approver": ""}, {"owner_approval": "nope"}, {"reason": " "}]
)
def test_missing_approver_bad_token_or_reason_blocks(override: dict[str, Any]) -> None:
    _blocked(_fake(_waiver(**override)))


def test_running_workflows_block() -> None:
    _blocked(_fake(running=1))


def test_evidence_hash_mismatch_or_missing_blocks() -> None:
    _blocked(_fake(_waiver(evidence=[{"path": EVIDENCE_PATH, "sha256": "0" * 64}])))
    _blocked(_fake(_waiver(evidence=[{"path": "absent.txt", "sha256": "0" * 64}])))
    _blocked(_fake(_waiver(evidence=[])))
    _blocked(_fake(_waiver(evidence="x")))


def test_collector_failure_is_unknown() -> None:
    f = _fake(rows=rp.CollectorUnavailable("db down"))
    f.pending = 1
    assert _check(f).status is rp.Status.UNKNOWN


# ---------------------------------------------------------------- 集約・承認・不変


def test_waived_on_a_non_waivable_check_is_blocked() -> None:
    names = rp.PRE_DEPLOY_REQUIRED
    results = [rp.CheckResult(n, rp.Status.PASS, "ok") for n in names]
    results = [
        rp.CheckResult(r.name, rp.Status.WAIVED, "w") if r.name == rp.CHECK_BACKUP else r
        for r in results
    ]
    assert rp.aggregate(results, names) is rp.Overall.BLOCKED


def test_waived_counts_as_satisfied_only_for_reservations() -> None:
    names = rp.PRE_DEPLOY_REQUIRED
    results = [
        rp.CheckResult(n, rp.Status.WAIVED if n == rp.CHECK_RESERVATIONS else rp.Status.PASS, "")
        for n in names
    ]
    assert rp.aggregate(results, names) is rp.Overall.READY
    assert frozenset({rp.CHECK_RESERVATIONS}) == rp.WAIVABLE_CHECKS


def _reviewed_with(status_by_name: dict[str, str]) -> str:
    doc = json.loads(_reviewed_report())
    for r in doc["results"]:
        r["status"] = status_by_name.get(r["name"], r["status"])
    return json.dumps(doc)


def test_approved_report_accepts_waived_reservations_only() -> None:
    ok = _reviewed_with({rp.CHECK_RESERVATIONS: "WAIVED"})
    f = Fake(texts={"approval.json": _approval(report=ok), "report.json": ok})
    assert rp.run_preflight(_request(), f, NOW).results  # 実行できる
    approval = next(
        r for r in rp.run_preflight(_request(), f, NOW).results if r.name == rp.CHECK_APPROVAL
    )
    assert approval.status is rp.Status.PASS, approval.detail

    bad = _reviewed_with({rp.CHECK_BACKUP: "WAIVED"})
    f2 = Fake(texts={"approval.json": _approval(report=bad), "report.json": bad})
    approval2 = next(
        r for r in rp.run_preflight(_request(), f2, NOW).results if r.name == rp.CHECK_APPROVAL
    )
    assert approval2.status is rp.Status.BLOCKED


def test_other_checks_are_identical_with_and_without_the_waiver_file() -> None:
    with_w = _report(_fake())
    without = _report(_fake(waiver=None))
    a = {r.name: r for r in with_w.results if r.name != rp.CHECK_RESERVATIONS}
    b = {r.name: r for r in without.results if r.name != rp.CHECK_RESERVATIONS}
    assert a == b


def test_text_and_json_show_waived_with_a_warning() -> None:
    report = _report(_fake())
    assert "WAIVED" in rp.render_text(report)
    doc = json.loads(rp.report_json(report))
    r = next(x for x in doc["results"] if x["name"] == rp.CHECK_RESERVATIONS)
    assert r["status"] == "WAIVED" and r["data"]["warning"]
    assert "WARNING" in rp.render_text(report)


def test_default_waiver_path_and_fingerprint_are_stable() -> None:
    assert rp.default_waiver_path(SHA) == f"release/waivers/{SHA}-reservations.json"
    assert rp.reservation_fingerprint(_view()) == rp.reservation_fingerprint(_view())
    assert rp.reservation_fingerprint(_view()) != rp.reservation_fingerprint(
        replace(_view(), estimated_cost_usd="1")
    )
