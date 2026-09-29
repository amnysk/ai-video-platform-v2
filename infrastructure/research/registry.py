"""設定（``RESEARCH_PROVIDER``）から Provider・Handler・見積もりを組む（ADR-0037 §6 / §8）。

- ``fake``: ``FakeSearchProvider`` / ``FakeContentFetcher``（固定コーパス。実ネットワークに出ない）
- ``none``（既定）: 何も組まない。依頼は外部を呼ばずに ``blocked``（fail-closed）
- **実 Web / YouTube の配線はしない**（``YouTubeSearchProvider`` / ``HttpContentFetcher`` を
  ここで組まない。``tests/architecture/test_research_isolation.py`` が検査する）。実 Provider を
  足すときは所有者の判断と ADR を先に置き、``provider_is_real`` が真になるので金額・quota の上限が
  必須になる（ADR-0037 §6）
- 種別ごとの Handler: Evidence（ADR-0038。``EvidenceHandler``。純粋な判断だけなので設定に依らず
  登録する。``none`` の依頼は実行器の門で先に ``blocked``）。Trend（ADR-0039）はまだ無く、
  実行器は Handler の無い種別の依頼を ``blocked``（``handler_not_available``）にする
- LLM 向けの Port（ADR-0038）: ``fake`` は ``FakeEvidenceAssessor`` / ``FakeClaimExtractor``、
  ``none`` は組まない。実 LLM は配線しない（評価は台帳 ``ResearchCall.ASSESS`` を通る）
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from contracts.research import (
    PROVIDER_CONFIG_VERSION,
    ResearchCall,
    ResearchKind,
)
from domain.research.evidence_handler import EvidenceHandler
from domain.research.evidence_ports import ClaimExtractor, EvidenceAssessor
from domain.research.handlers import ResearchHandler
from domain.research.ports import ContentFetcher, SearchKind, SearchProvider
from infrastructure.config import Settings
from infrastructure.research.fake_evidence import FakeClaimExtractor, FakeEvidenceAssessor
from infrastructure.research.fake_providers import FakeContentFetcher, FakeSearchProvider
from infrastructure.research.quota_costs import YOUTUBE_FULL_SEARCH_UNITS

__all__ = [
    "FAKE_PROVIDER",
    "NONE_PROVIDER",
    "CostModel",
    "ResearchProviders",
    "build_claim_extractor",
    "build_cost_model",
    "build_handlers",
    "build_providers",
    "provider_config_version",
    "provider_is_configured",
    "provider_is_real",
]

FAKE_PROVIDER = "fake"
NONE_PROVIDER = "none"
#: 外部の有料 API・quota を使わない設定。これ以外（将来の実 Provider）は上限が必須になる
_NON_REAL_PROVIDERS = frozenset({FAKE_PROVIDER, NONE_PROVIDER})
#: 呼び出せる Provider を組める設定
_CONFIGURED_PROVIDERS = frozenset({FAKE_PROVIDER})


def provider_is_real(mode: str) -> bool:
    """実 Provider（外部の有料 API・quota を使いうる）か。

    未知の設定は実物として扱う（止める側）。
    """
    return mode not in _NON_REAL_PROVIDERS


def provider_is_configured(mode: str) -> bool:
    """呼び出せる Provider を組めるか。``none`` と未知の設定は偽（止める側）。"""
    return mode in _CONFIGURED_PROVIDERS


def provider_config_version(mode: str) -> str:
    """``request_hash`` に入る版。Fake の結果を別の Provider 設定の依頼に再利用させない。"""
    return f"{PROVIDER_CONFIG_VERSION}+{mode}"


@dataclass(frozen=True, slots=True)
class ResearchProviders:
    """実行器が使う Provider 一式。``configured`` が偽なら依頼は ``blocked``（fail-closed）。"""

    #: 設定値（``fake`` / ``none``。ログ・台帳の ``provider`` 列の診断用）
    mode: str
    search: SearchProvider | None
    #: URL の本文取得。Tier A の ``HttpContentFetcher``（``UrlGuard`` を必ず通る）か
    #: ``FakeContentFetcher`` だけを入れる。実行器は URL をこの Port 以外で取得しない
    fetcher: ContentFetcher | None
    is_real: bool
    #: Evidence の評価器（ADR-0038）。``None`` なら評価せず、評価が要る claim は ``insufficient``
    #: のまま依頼は ``partial``（``assessor_not_available``）。合格にしない
    assessor: EvidenceAssessor | None = None

    @property
    def configured(self) -> bool:
        return self.search is not None and self.fetcher is not None


@dataclass(frozen=True, slots=True)
class CostModel:
    """金額・quota の**見積もり**（呼ぶ前に台帳へ載せる。ADR-0037 §4）。

    金額の単価はまだ無い（実 Provider が無いので設定値も持たない。空 = 見積もらない）。
    YouTube の quota 単位は ``quota_costs.py``（Adapter と Fake が共有する唯一の定義）。
    """

    usd_per_call: Mapping[ResearchCall, Decimal] = field(default_factory=dict)
    youtube_units_per_search: int = YOUTUBE_FULL_SEARCH_UNITS

    def usd_for(self, call: ResearchCall) -> Decimal | None:
        return self.usd_per_call.get(call)

    def quota_units_for(self, kind: SearchKind) -> int | None:
        return self.youtube_units_per_search if kind == "youtube" else None


def build_providers(settings: Settings) -> ResearchProviders:
    mode = settings.research_provider
    if mode == FAKE_PROVIDER:
        return ResearchProviders(
            mode=mode,
            search=FakeSearchProvider(),
            fetcher=FakeContentFetcher(),
            is_real=provider_is_real(mode),
            assessor=FakeEvidenceAssessor(),
        )
    return ResearchProviders(mode=mode, search=None, fetcher=None, is_real=provider_is_real(mode))


def build_handlers(settings: Settings) -> Mapping[ResearchKind, ResearchHandler]:
    """種別ごとの Handler。Evidence は ADR-0038、Trend は ADR-0039 の段で足す。"""
    del settings
    return {ResearchKind.EVIDENCE: EvidenceHandler()}


def build_claim_extractor(settings: Settings) -> ClaimExtractor | None:
    """台本の照合の上乗せ抽出器（ADR-0038 §4）。``fake`` だけが組める。無くても決定的な網は走る。"""
    return FakeClaimExtractor() if settings.research_provider == FAKE_PROVIDER else None


def build_cost_model(settings: Settings) -> CostModel:
    del settings
    return CostModel()
