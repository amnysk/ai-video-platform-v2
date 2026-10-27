"""月間 hard cap が迂回できない結線であること（INV-86 / ADR-0063）。

理由は docs/testing/fal-monthly-cap-rationale.md。cap を持たない fal の予約経路が
後から増えると、月の上限は黙って効かなくなる。ここはそれを機械的に止める。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from contracts.provider_health import PROVIDER_FAMILY, ProviderFamily
from contracts.states import ProviderCall
from infrastructure.db.repositories import ProviderReservationRepository

REPO = pathlib.Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = ("apps", "workers", "infrastructure", "scripts", "domain", "contracts")
FAL_PROVIDERS = {p for p, f in PROVIDER_FAMILY.items() if f is ProviderFamily.FAL}


def _production_files():
    for root in PRODUCTION_ROOTS:
        yield from (REPO / root).rglob("*.py")


def _attribute_calls(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        ):
            yield node


def test_reserve_accepts_the_cap_and_now_keywords_with_safe_defaults() -> None:
    import inspect

    params = inspect.signature(ProviderReservationRepository.reserve).parameters
    assert params["monthly_cap_usd"].default is None
    assert params["now"].default is None
    assert params["monthly_cap_usd"].kind is inspect.Parameter.KEYWORD_ONLY


def test_paid_job_is_the_only_production_path_that_can_reserve_a_fal_provider() -> None:
    """``ProviderReservationRepository.reserve`` を呼ぶ production のファイルを数える。

    fal を予約できるのは paid_job.py だけで、そこは必ず cap を渡す。他に reserve を呼ぶ
    ファイル（codex / youtube の台帳）は fal の ProviderCall を一切参照しない。
    """
    callers: dict[str, list[ast.Call]] = {}
    for path in _production_files():
        text = path.read_text()
        if ".reserve(" not in text:
            continue
        tree = ast.parse(text)
        calls = list(_attribute_calls(tree, "reserve"))
        if calls:
            callers[str(path.relative_to(REPO))] = calls

    fal_refs = re.compile(r"FAL_IMAGE|FAL_VIDEO|PROVIDER_FAMILY|ProviderFamily\.FAL")
    fal_capable = {
        rel
        for rel in callers
        if rel == "infrastructure/production/paid_job.py"
        or fal_refs.search((REPO / rel).read_text())
        or "spec.provider" in (REPO / rel).read_text()
    }
    # research の台帳は別テーブル・別 repository（provider_reservations ではない）
    fal_capable -= {"infrastructure/research/executor.py"}
    assert fal_capable == {"infrastructure/production/paid_job.py"}, fal_capable

    for call in callers["infrastructure/production/paid_job.py"]:
        keywords = {kw.arg for kw in call.keywords}
        assert "monthly_cap_usd" in keywords, "paid_job の reserve は cap を渡さなければならない"
        assert "estimated_cost_usd" in keywords


def test_non_fal_reserve_callers_never_name_a_fal_provider() -> None:
    for rel in (
        "workers/planning/activities.py",
        "workers/storyboard/activities.py",
        "workers/production/scene_recovery_activities.py",
        "workers/upload/activities.py",
    ):
        src = (REPO / rel).read_text()
        for provider in FAL_PROVIDERS:
            assert provider.name not in src, f"{rel} references {provider.name}"


def test_the_cap_providers_come_from_the_single_family_table() -> None:
    assert {ProviderCall.FAL_IMAGE, ProviderCall.FAL_VIDEO} == FAL_PROVIDERS
    # 月間集計・状態表示・resume のどれも provider の集合を再 literal しない（AGENTS §8）
    for rel in (
        "infrastructure/db/repositories.py",
        "infrastructure/production/provider_credits.py",
        "domain/budget_gate.py",
    ):
        src = (REPO / rel).read_text()
        literal = r"\[\s*ProviderCall\.FAL_IMAGE\s*,\s*ProviderCall\.FAL_VIDEO"
        assert not re.search(literal, src), rel
        assert not re.search(r'["\']fal_image["\']\s*,\s*["\']fal_video["\']', src), rel


def test_the_utc_month_start_is_defined_in_exactly_one_place() -> None:
    """``replace(day=1, hour=0 ...)`` で月初を作る production コードは 1 箇所だけ。"""
    pattern = re.compile(r"replace\(\s*day\s*=\s*1\b")
    defining = sorted(
        str(p.relative_to(REPO)) for p in _production_files() if pattern.search(p.read_text())
    )
    assert len(defining) == 1, defining


def test_provider_credits_month_start_is_that_single_function() -> None:
    from datetime import UTC, datetime, timedelta, timezone

    from infrastructure.production import provider_credits

    jst = timezone(timedelta(hours=9))
    moment = datetime(2026, 10, 1, 0, 30, tzinfo=jst)  # UTC では 9/30
    assert provider_credits.month_start(moment) == datetime(2026, 9, 1)
    assert provider_credits.month_start(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == datetime(
        2026, 12, 1
    )


def test_the_cap_arithmetic_has_no_floats() -> None:
    src = (REPO / "domain/budget_gate.py").read_text()
    tree = ast.parse(src)
    floats = [
        n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)
    ]
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "float"
    ]
    assert floats == [] and calls == []


@pytest.mark.parametrize(
    "path", ["workers/production_image/run_worker.py", "workers/production_video/run_worker.py"]
)
def test_the_workers_still_hand_the_runner_the_decision_that_carries_the_cap(path: str) -> None:
    """cap は BudgetDecision.cap_usd 経由で渡る。worker が別の配線（cap を落とす）を持たない。"""
    src = (REPO / path).read_text()
    assert "budget=settings.fal_budget" in src
    assert "monthly_cap" not in src  # cap の値を worker が直接いじらない


def test_no_workflow_or_activity_payload_mentions_the_cap() -> None:
    """workflow のコード・シリアライズされる payload を変えない（replay 互換 / INV-40）。"""
    offenders = []
    for path in (REPO / "workers").rglob("*.py"):
        text = path.read_text()
        if "@workflow.defn" in text and re.search(r"monthly_cap|CapDecision|cap_usd", text):
            offenders.append(str(path.relative_to(REPO)))
    for rel in ("contracts/production_activities.py", "contracts/pipeline.py"):
        path = REPO / rel
        if path.exists() and re.search(r"monthly_cap|CapDecision|cap_usd", path.read_text()):
            offenders.append(rel)
    assert offenders == []
