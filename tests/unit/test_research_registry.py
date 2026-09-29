"""Research の Provider の組み立て（ADR-0037 §6 / §8）。知っているのは ``fake`` と ``none`` だけ。

理由は docs/testing/research-execution-rationale.md。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.research import RESEARCH_PROVIDER_MODES
from infrastructure.config import Settings
from infrastructure.research.fake_providers import FakeContentFetcher, FakeSearchProvider
from infrastructure.research.quota_costs import YOUTUBE_FULL_SEARCH_UNITS
from infrastructure.research.registry import (
    build_cost_model,
    build_handlers,
    build_providers,
    provider_config_version,
    provider_is_configured,
    provider_is_real,
)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_the_default_provider_is_none_and_builds_nothing_that_can_call_out() -> None:
    """既定は ``none``（fail-closed）。.env や環境変数に左右されない宣言上の既定を見る。"""
    assert Settings.model_fields["research_provider"].default == "none"
    providers = build_providers(_settings())
    assert providers.mode == "none"
    assert providers.search is None and providers.fetcher is None
    assert providers.configured is False
    assert providers.is_real is False


def test_fake_builds_the_fixed_corpus_providers() -> None:
    providers = build_providers(_settings(research_provider="fake"))
    assert isinstance(providers.search, FakeSearchProvider)
    assert isinstance(providers.fetcher, FakeContentFetcher)
    assert providers.configured is True
    assert providers.is_real is False


def test_only_fake_and_none_are_accepted() -> None:
    assert set(RESEARCH_PROVIDER_MODES) == {"fake", "none"}
    with pytest.raises(ValidationError):
        _settings(research_provider="youtube")


def test_unknown_modes_count_as_real_and_unconfigured() -> None:
    """将来の実 Provider は、登録されるまで「実物・未設定」として扱う（どちらも止める側）。"""
    assert provider_is_real("fake") is False and provider_is_real("none") is False
    assert provider_is_real("some-future-provider") is True
    assert provider_is_configured("fake") is True
    assert provider_is_configured("none") is False
    assert provider_is_configured("some-future-provider") is False


def test_the_provider_config_version_separates_fake_results_from_other_modes() -> None:
    """Fake の結果を別の Provider 設定の依頼に再利用させない（``request_hash`` に入る）。"""
    assert provider_config_version("fake") != provider_config_version("none")
    assert provider_config_version("fake").endswith("+fake")


def test_no_kind_specific_handler_is_registered_yet() -> None:
    """Trend / Evidence の Handler は後続の段で登録する。未登録の種別は実行器が blocked にする。"""
    assert dict(build_handlers(_settings(research_provider="fake"))) == {}


def test_the_cost_model_estimates_youtube_quota_from_the_shared_constant() -> None:
    model = build_cost_model(_settings())
    assert model.youtube_units_per_search == YOUTUBE_FULL_SEARCH_UNITS
    assert model.quota_units_for("youtube") == YOUTUBE_FULL_SEARCH_UNITS
    assert model.quota_units_for("web") is None
    assert model.usd_per_call == {}
