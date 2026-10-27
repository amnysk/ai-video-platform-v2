"""release 限定の未照合予約 waiver の安全性（ADR-0062 / INV-85）。独立レビュー側の検査。

waiver は台帳を書かず・再送を許さず・他のチェックを緩めない。それを別の角度から固定する。
各テストが存在する理由は docs/testing/release-reservation-waiver-rationale.md。
"""

from __future__ import annotations

import ast
import importlib
import json
import pathlib
import re
from dataclasses import replace
from typing import Any

import pytest

from domain import release_preflight as rp
from domain.budget_gate import evaluate_budget
from tests.unit.test_release_preflight import NOW, SHA, _request, sha256_text
from tests.unit.test_release_reservation_waiver import (
    EVIDENCE_PATH,
    EVIDENCE_TEXT,
    WAIVER_PATH,
    WFake,
    _check,
    _fake,
    _report,
    _waiver,
)

REPO = pathlib.Path(__file__).resolve().parents[2]

#: 本番で実際に未照合のまま残っている予約の形（2026-10-04 の fal_video）
PROD_VIEW = rp.ReservationView(
    id="591d1a8b-ab05-4d9b-b8f7-befad36cc2db",
    provider="fal_video",
    status="reserved",
    provider_request_id="01a108c3-af05-7fc0-b5cd-42d24f8105dc",
    estimated_cost_usd="1.9352",
    reserved_at="2026-10-04T21:13:23.570964+00:00",
    dispatched_at="2026-10-04T21:13:23.585740+00:00",
    raw_output_key=None,
    outcome_artifact_id=None,
)


def _prod_fake(**kw: Any) -> WFake:
    waiver = _waiver(
        reservation_id=PROD_VIEW.id,
        provider_request_id=PROD_VIEW.provider_request_id,
        reservation_fingerprint=rp.reservation_fingerprint(PROD_VIEW),
    )
    return _fake(waiver, rows=[PROD_VIEW], **kw)


def test_production_shaped_reservation_is_waived() -> None:
    r = _check(_prod_fake())
    assert r.status is rp.Status.WAIVED, r.detail
    assert r.data["reservation_id"] == PROD_VIEW.id
    assert r.data["provider_request_id"] == PROD_VIEW.provider_request_id


def test_production_shaped_reservation_is_not_waived_for_a_different_request_id() -> None:
    other = replace(PROD_VIEW, provider_request_id="01a108c3-af05-7fc0-b5cd-000000000000")
    f = _prod_fake()
    f.rows = [other]
    assert _check(f).status is rp.Status.BLOCKED


# ---------------------------------------------------------------- (8) 台帳に触れない


class _Recording(WFake):
    """公開メソッドの呼び出しを記録する。preflight が呼んだ名前を後で検査する。"""

    def __getattribute__(self, name: str) -> Any:
        attr = super().__getattribute__(name)
        if callable(attr) and not name.startswith("_") and name not in {"get"}:
            object.__getattribute__(self, "calls").add(name)
        return attr

    calls: set[str]


_WRITE_VERB = re.compile(
    r"^(write|insert|update|delete|set_|save|store|reconcile|mark|resolve|submit|resubmit|"
    r"create|apply|execute|release|reserve|terminate|signal|pause|unpause|trigger)"
)


def _protocol_methods() -> set[str]:
    return {n for n, v in vars(rp.Collectors).items() if callable(v) and not n.startswith("_")}


def test_collectors_protocol_has_no_write_shaped_method() -> None:
    methods = _protocol_methods()
    assert {"pending_reservations", "paid_budget_setting"} <= methods
    assert not {m for m in methods if _WRITE_VERB.match(m)}


def test_a_waived_run_calls_only_protocol_read_methods() -> None:
    f = _Recording(rows=[rp.ReservationView(**vars(PROD_VIEW))])
    object.__setattr__(f, "calls", set())
    waiver = _waiver(
        reservation_id=PROD_VIEW.id,
        provider_request_id=PROD_VIEW.provider_request_id,
        reservation_fingerprint=rp.reservation_fingerprint(PROD_VIEW),
    )
    f.texts[WAIVER_PATH] = json.dumps(waiver)
    report = rp.run_preflight(_request(reservation_waiver=WAIVER_PATH), f, NOW)
    r = next(x for x in report.results if x.name == rp.CHECK_RESERVATIONS)
    assert r.status is rp.Status.WAIVED, r.detail
    called = object.__getattribute__(f, "calls") - {"calls"}
    assert called <= _protocol_methods(), called - _protocol_methods()
    assert {"pending_reservations", "paid_budget_setting"} <= called
    assert not {m for m in called if _WRITE_VERB.match(m)}


def test_the_new_collector_sql_is_one_select_and_collectors_are_in_the_read_only_scan() -> None:
    path = REPO / "infrastructure" / "release" / "collectors.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "pending_reservations"
    )
    call = next(
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_select_all"
    )
    sql = "".join(
        n.value for n in ast.walk(call) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    assert sql.lstrip().upper().startswith("SELECT"), sql
    assert ";" not in sql
    assert len(re.findall(r"\bSELECT\b", sql, re.IGNORECASE)) == 1
    assert not re.search(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|FOR\s+UPDATE)\b", sql, re.IGNORECASE
    )
    arch = (REPO / "tests" / "architecture" / "test_release_preflight_read_only.py").read_text(
        encoding="utf-8"
    )
    assert '"collectors.py"' in arch


# ---------------------------------------------------------------- (9) 再送は許されない


def test_waiver_requires_a_budget_that_forbids_submit() -> None:
    from decimal import Decimal

    assert evaluate_budget(Decimal("0")).submit_allowed is False
    assert evaluate_budget(Decimal("5")).submit_allowed is True
    # 予算が 0 でなければ waiver は成立しない（再送が許される状態では免除しない）
    assert _check(_fake(budget="5")).status is rp.Status.BLOCKED
    assert _check(_fake(budget=None)).status is rp.Status.BLOCKED


@pytest.mark.parametrize(
    ("module", "test_name"),
    [
        (
            "tests.unit.test_provider_credits_script",
            "test_resume_is_refused_for_the_production_unreconciled_reservation_state",
        ),
        ("tests.unit.test_paid_job_budget", "test_runner_built_without_a_budget_fails_closed"),
        ("tests.unit.test_budget_gate_domain", "test_positive_budget_passes_this_gate_only"),
    ],
)
def test_the_resubmit_guards_that_the_waiver_relies_on_still_exist(
    module: str, test_name: str
) -> None:
    """waiver の安全性は既存の再送ガードに依存する。ガードのテストが消えたら落ちる。"""
    mod = importlib.import_module(module)
    assert callable(getattr(mod, test_name, None)), f"{module}::{test_name} is gone"


# ---------------------------------------------------------------- (10) 他を緩めない

#: 1 項目ずつ壊す。BLOCKED / UNKNOWN になるものだけ（既定の Fake は全項目良好）
FAULTS: dict[str, dict[str, Any]] = {
    "commit_missing": {"commit_ok": False},
    "head_dirty": {"head": rp.HeadState(head_sha=SHA, dirty=True)},
    "head_other": {"head": rp.HeadState(head_sha="d" * 40, dirty=False)},
    "relation_diverged": {"relation": rp.Relation.DIVERGED},
    "ci_failed": {"ci": [rp.CheckRun(rp.CI_GATE_JOB_NAME, "completed", "failure")]},
    "ci_unreadable": {"ci": rp.CollectorUnavailable("gh down")},
    "alembic_unreadable": {"alembic": rp.CollectorUnavailable("db down")},
    "containers_unreadable": {"revisions": rp.CollectorUnavailable("docker down")},
    "unhealthy": {"unhealthy": ["api"]},
    "schedules_empty": {"schedules": {}},
    "schedules_unreadable": {"schedules": rp.CollectorUnavailable("temporal down")},
    "texts_missing": {"texts": {}},
}


def _non_reservation(report: rp.Report) -> dict[str, tuple[rp.Status, str]]:
    return {r.name: (r.status, r.detail) for r in report.results if r.name != rp.CHECK_RESERVATIONS}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_a_valid_waiver_does_not_relax_any_other_check(fault: str) -> None:
    def build(with_waiver: bool) -> WFake:
        kw = dict(FAULTS[fault])
        f = _fake(_waiver() if with_waiver else None, **kw)
        f.texts.setdefault(EVIDENCE_PATH, EVIDENCE_TEXT)
        return f

    with_w = _report(build(True))
    without = _report(build(False))
    assert _non_reservation(with_w) == _non_reservation(without)
    assert with_w.overall is not rp.Overall.READY
    assert any(
        r.status in (rp.Status.BLOCKED, rp.Status.UNKNOWN)
        for r in with_w.results
        if r.name != rp.CHECK_RESERVATIONS
    ), "the fault must actually break some other check"


def test_the_fault_matrix_covers_several_distinct_checks() -> None:
    broken: set[str] = set()
    for kw in FAULTS.values():
        report = _report(_fake(_waiver(), **kw))
        broken |= {
            r.name
            for r in report.results
            if r.name != rp.CHECK_RESERVATIONS and r.status is not rp.Status.PASS
        }
    assert len(broken) >= 6, broken


def test_waiver_is_ignored_in_post_deploy_mode() -> None:
    def run(f: WFake) -> rp.Report:
        return rp.run_preflight(
            _request(reservation_waiver=WAIVER_PATH, verify_post_deploy=True), f, NOW
        )

    with_w = run(_fake())
    without = run(_fake(None))
    assert with_w.results == without.results
    assert all(r.name != rp.CHECK_RESERVATIONS for r in with_w.results)
    assert all(r.status is not rp.Status.WAIVED for r in with_w.results)


def test_a_waiver_bound_to_sha_a_does_not_apply_to_sha_b() -> None:
    sha_b = "c" * 40
    f = _fake(_waiver(sha=SHA))
    report = rp.run_preflight(_request(target_sha=sha_b, reservation_waiver=WAIVER_PATH), f, NOW)
    r = next(x for x in report.results if x.name == rp.CHECK_RESERVATIONS)
    assert r.status is rp.Status.BLOCKED and "different SHA" in r.detail
    assert report.overall is not rp.Overall.READY


def test_the_raw_owner_token_never_appears_in_any_output() -> None:
    f = _prod_fake()
    report = _report(f)
    token = _waiver()["owner_approval"]
    assert token not in rp.render_text(report) + rp.report_json(report)
    assert sha256_text(token)[:16] in rp.report_json(report)
