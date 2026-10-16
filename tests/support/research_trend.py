"""Trend Research のテスト部品（ADR-0039。テスト専用）。

依頼の作成・実行器の組み立て・保存済み成果物の読み出しを 1 か所にまとめる。Fake Provider と
固定コーパス（``infrastructure/research/fake_corpus.py``）だけを使い、実ネットワークに出ない。

固定コーパスの YouTube 動画の公開日（2026-08-10 / 08-25 / 07-01 / 09-01）に合わせ、基準日時は
2026-08-30。7 日の窓（08-23〜）には ``YT_SEKIGAHARA_SHORT`` だけが、30 日の窓（07-31〜）には
``YT_SEKIGAHARA_LONG`` と ``YT_SEKIGAHARA_SHORT`` が入る（同じ動画を 2 つの検索で観測する）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from contracts.research import ResearchArtifactType, parse_research_spec
from contracts.research_trend import TrendArtifact, parse_trend_artifact
from domain.research.ports import SearchQuery, SearchResults
from domain.research.trend_handler import TrendHandler
from domain.research.trend_ports import TrendInterpreter
from infrastructure.db.research_repositories import (
    ResearchArtifactRepository,
    ResearchRequestRepository,
)
from infrastructure.research.executor import ResearchExecutor
from infrastructure.research.fake_corpus import (
    YT_SEKIGAHARA_SHORT,
    default_corpus,
    with_view_counts,
)
from infrastructure.research.fake_providers import FakeContentFetcher, FakeSearchProvider
from infrastructure.research.fake_trend import FakeTrendInterpreter
from infrastructure.research.registry import CostModel, ResearchProviders
from tests.support.research import trend_payload

PCV = "provider-config-1+fake"
TREND_AS_OF = datetime(2026, 8, 30, 0, 0, tzinfo=UTC)
#: 同じ動画の 2 時点の観測（差分の検査用）
FIRST_SEEN = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
SECOND_SEEN = FIRST_SEEN + timedelta(hours=10)
SHORT_VIEWS_FIRST = 900_000
#: 既定のコーパスの ``YT_SEKIGAHARA_SHORT`` の再生数
SHORT_VIEWS_SECOND = 980_000


def trend_request_payload(**overrides: Any) -> dict[str, Any]:
    payload = trend_payload(
        language="ja",
        format_profile="shorts",
        as_of=TREND_AS_OF.isoformat(),
        time_window={
            "start": datetime(2026, 6, 1, tzinfo=UTC).isoformat(),
            "end": TREND_AS_OF.isoformat(),
        },
    )
    payload["inputs"] = {
        "region": "JP",
        "audience_hypothesis": "日本史に関心のある成人",
        "seed_terms": ["関ヶ原"],
    }
    payload.update(overrides)
    return payload


async def make_trend_request(
    session_factory, payload: dict[str, Any] | None = None, key: str = "trend:b5"
) -> str:
    async with session_factory() as session:
        created = await ResearchRequestRepository(session).create_or_get(
            idempotency_key=key,
            spec=parse_research_spec(payload or trend_request_payload()),
            provider_config_version=PCV,
        )
        await session.commit()
    return created.id


class TwoTimeSearch:
    """1 回目の検索は ``FIRST_SEEN``、2 回目以降は ``SECOND_SEEN`` に観測した統計を返す。

    ``FakeSearchProvider(stat_observed_at=)`` と ``fake_corpus.with_view_counts`` で、同じ動画の
    2 時点の観測（再生数が増えた）を作る。
    """

    name = "fake"

    def __init__(self) -> None:
        earlier = with_view_counts(default_corpus(), {YT_SEKIGAHARA_SHORT: SHORT_VIEWS_FIRST})
        self.first = FakeSearchProvider(earlier, stat_observed_at=FIRST_SEEN)
        self.later = FakeSearchProvider(stat_observed_at=SECOND_SEEN)
        self.calls: list[SearchQuery] = []

    async def search(self, query: SearchQuery) -> SearchResults:
        self.calls.append(query)
        provider = self.first if len(self.calls) == 1 else self.later
        return await provider.search(query)


def trend_providers(
    *,
    search: Any | None = None,
    interpreter: TrendInterpreter | None = None,
    with_interpreter: bool = True,
) -> ResearchProviders:
    return ResearchProviders(
        mode="fake",
        search=search or FakeSearchProvider(),
        fetcher=FakeContentFetcher(),
        is_real=False,
        interpreter=(interpreter or FakeTrendInterpreter()) if with_interpreter else None,
    )


def trend_executor(
    session_factory, store, providers: ResearchProviders | None = None
) -> ResearchExecutor:
    handler = TrendHandler()
    return ResearchExecutor(
        session_factory=session_factory,
        store=store,
        bucket="artifacts",
        providers=providers or trend_providers(),
        handlers={handler.kind: handler},
        cost_model=CostModel(),
        clock=lambda: datetime.now(UTC),
    )


async def stored_trend(session_factory, store, request_id: str) -> tuple[Any, TrendArtifact]:
    """(現行の成果物の行, 本体)。"""
    async with session_factory() as session:
        record = await ResearchArtifactRepository(session).find_current(
            request_id, ResearchArtifactType.RESEARCH_TREND
        )
    assert record is not None
    return record, parse_trend_artifact(await store.get_json(record.object_key))
