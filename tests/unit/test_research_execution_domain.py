"""Research の実行の純粋な部分（ADR-0037 §6 / §8）。

予算の門、計画の検査、URL、生データのキー、失敗の分類。

理由は docs/testing/research-execution-rationale.md。
"""

from __future__ import annotations

import importlib
import pkgutil
from decimal import Decimal

import pytest

from contracts.research import ResearchCall, ResearchLimits, ResearchStopCode
from contracts.states import FailureClass
from domain.errors import (
    FAILURE_CLASS_BY_TYPE_NAME,
    NON_RETRYABLE_ERROR_TYPE_NAMES,
    DomainError,
    classify_failure,
)
from domain.research import errors as research_errors
from domain.research.admission import admission_block, lacks_budget
from domain.research.handlers import SearchStep, dedupe_fetch_targets, plan_within_ceiling
from domain.research.keys import RESEARCH_KEY_PREFIX, research_raw_object_key
from domain.research.ports import SearchHit, SearchQuery
from domain.research.urls import normalize_url

REQUEST_ID = "7d1c7a53-3a8e-5e0b-9e53-1c2b3d4e5f60"
CALL_ID = "0b6a3c55-1d2e-4f70-8a9b-0c1d2e3f4a5b"
BOTH = ResearchLimits(max_cost_usd=Decimal("1.00"), max_youtube_units=500)


# ------------------------------------------------------------------ 予算の門（ADR-0037 §6）


@pytest.mark.parametrize(
    ("limits", "real", "expected"),
    [
        (ResearchLimits(), True, True),
        (ResearchLimits(max_cost_usd=Decimal("1.00")), True, True),
        (ResearchLimits(max_youtube_units=500), True, True),
        (BOTH, True, False),
        (ResearchLimits(), False, False),
    ],
)
def test_a_real_provider_needs_both_money_and_quota_limits(
    limits: ResearchLimits, real: bool, expected: bool
) -> None:
    """「どちらか一方でも」未設定なら止める（旧実装の文章とコードの食い違いを厳しい側に揃えた）。"""
    assert lacks_budget(limits, provider_is_real=real) is expected


def test_admission_blocks_an_unconfigured_provider_before_the_budget() -> None:
    reason = admission_block(ResearchLimits(), provider_configured=False, provider_is_real=False)
    assert reason is not None and reason.code is ResearchStopCode.PROVIDER_NOT_CONFIGURED
    assert reason.as_blocked_reason()["code"] == "provider_not_configured"

    unbudgeted = admission_block(ResearchLimits(), provider_configured=True, provider_is_real=True)
    assert unbudgeted is not None and unbudgeted.code is ResearchStopCode.BUDGET_NOT_SET

    assert admission_block(BOTH, provider_configured=True, provider_is_real=True) is None
    assert (
        admission_block(ResearchLimits(), provider_configured=True, provider_is_real=False) is None
    )


# ------------------------------------------------------------------ 計画（純粋）


def _step(step_id: str, text: str = "q") -> SearchStep:
    return SearchStep(
        step_id=step_id, query=SearchQuery(text=text, kind="web", max_results=3), purpose="p"
    )


def test_the_plan_is_cut_at_the_ceiling_and_the_rest_is_reported_skipped() -> None:
    run, skipped = plan_within_ceiling((_step("s01"), _step("s02"), _step("s03")), max_searches=2)
    assert [s.step_id for s in run] == ["s01", "s02"]
    assert skipped == ("s03",)


@pytest.mark.parametrize("ids", [("s01", "s01"), ("bad id",), ("",)])
def test_a_plan_with_duplicate_or_malformed_step_ids_is_rejected(ids: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        plan_within_ceiling(tuple(_step(i) for i in ids), max_searches=5)


def _hit(url: str) -> SearchHit:
    return SearchHit(url=url, title="t", snippet="s", published_at=None, provider="fake")


def test_fetch_targets_are_deduplicated_by_normalized_url_and_capped() -> None:
    from domain.research.handlers import FetchTarget

    targets = (
        FetchTarget(hit=_hit("HTTPS://Example.org:443/a#frag"), step_id="s01"),
        FetchTarget(hit=_hit("https://example.org/a"), step_id="s02"),
        FetchTarget(hit=_hit("https://example.org/b"), step_id="s02"),
        FetchTarget(hit=_hit("https://example.org/c"), step_id="s02"),
        FetchTarget(hit=_hit("https://example.org/done"), step_id="s02"),
    )
    chosen = dedupe_fetch_targets(
        targets, already_fetched={"https://example.org/done"}, remaining=2
    )
    assert [t.hit.url for t in chosen] == [
        "HTTPS://Example.org:443/a#frag",
        "https://example.org/b",
    ]
    assert dedupe_fetch_targets(targets, already_fetched=(), remaining=0) == ()


def test_normalize_url_folds_case_default_ports_and_fragments_but_keeps_the_query() -> None:
    assert normalize_url(" HTTP://A.example:80?b=2&a=1#x ") == "http://a.example/?b=2&a=1"
    assert normalize_url("https://a.example:8443/p") == "https://a.example:8443/p"
    assert normalize_url("not a url") == "not a url"


# ------------------------------------------------------------------ 生データのキー


def test_raw_provider_output_lives_under_the_research_prefix() -> None:
    key = research_raw_object_key(REQUEST_ID, ResearchCall.SEARCH, CALL_ID)
    assert key == f"{RESEARCH_KEY_PREFIX}/{REQUEST_ID}/raw/search/{CALL_ID}.json"
    assert not key.startswith("artifacts/")
    with pytest.raises(ValueError):
        research_raw_object_key("not-a-uuid", ResearchCall.SEARCH, CALL_ID)
    with pytest.raises(ValueError):
        research_raw_object_key(REQUEST_ID, ResearchCall.FETCH, "../../escape")


# ------------------------------------------------------------------ 失敗の分類（型名。Temporal）


def _research_error_classes() -> list[type[DomainError]]:
    package = importlib.import_module("domain.research")
    for info in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"domain.research.{info.name}")

    def walk(cls: type) -> list[type]:
        found: list[type] = []
        for sub in cls.__subclasses__():
            found.append(sub)
            found.extend(walk(sub))
        return found

    return sorted(
        {c for c in walk(DomainError) if c.__module__.startswith("domain.research")},
        key=lambda c: c.__name__,
    )


def test_every_research_error_lives_in_the_one_research_errors_module() -> None:
    """型名の表は ``domain/research/errors.py`` の import 時に作る。

    別のモジュールに置いた research の例外は表に載らない。
    """
    classes = _research_error_classes()
    assert classes, "the research errors must exist"
    assert {c.__module__ for c in classes} == {"domain.research.errors"}


def test_every_research_error_is_reachable_by_type_name_with_its_isinstance_class() -> None:
    for cls in _research_error_classes():
        expected = classify_failure(cls("probe"))
        assert research_errors.RESEARCH_FAILURE_CLASS_BY_TYPE_NAME[cls.__name__] is expected
        assert research_errors.research_failure_class_from_type_name(cls.__name__) is expected
        non_retryable = expected in {FailureClass.NEEDS_INPUT, FailureClass.PERMANENT}
        assert (cls.__name__ in research_errors.RESEARCH_NON_RETRYABLE_ERROR_TYPE_NAMES) is (
            non_retryable
        )


def test_research_type_names_do_not_shadow_the_base_table_and_it_stays_unchanged() -> None:
    """``domain/errors.py`` の表には足さない（ADR-0037 §8）。名前の衝突もさせない。"""
    research_names = set(research_errors.RESEARCH_FAILURE_CLASS_BY_TYPE_NAME)
    assert research_names.isdisjoint(FAILURE_CLASS_BY_TYPE_NAME)
    assert research_names.isdisjoint(NON_RETRYABLE_ERROR_TYPE_NAMES)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ResearchSourceUnavailableError", FailureClass.RETRYABLE),
        ("ResearchArtifactReadbackError", FailureClass.RETRYABLE),
        ("ResearchAmbiguousCallError", FailureClass.NEEDS_INPUT),
        ("ResearchIdempotencyConflictError", FailureClass.PERMANENT),
        ("ResearchOutputInvalidError", FailureClass.PERMANENT),
        # 基底の表にある名前はそのまま引ける。未知の名前は安全側（needs_input。INV-12）
        ("ScriptSchemaViolationError", FailureClass.RETRYABLE),
        ("SomethingUnknown", FailureClass.NEEDS_INPUT),
        (None, FailureClass.NEEDS_INPUT),
    ],
)
def test_the_research_lookup_falls_back_to_the_base_table(
    name: str | None, expected: FailureClass
) -> None:
    assert research_errors.research_failure_class_from_type_name(name) is expected
