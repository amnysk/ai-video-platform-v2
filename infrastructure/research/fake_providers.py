"""Fake Provider（固定コーパス。通常テストと worker の Fake 実行の唯一の Provider）。

- **実ネットワークに出ない**（``httpx`` / ``socket`` を import しない。テストが固定する）
- 決定的: 同じ query・URL は同じ結果。時刻は注入できる時計（既定は固定値）
- 呼び出し履歴（``calls``）と障害注入（``fail_next`` / ``fail_with_429``）を持つ。
  障害注入は本物の Adapter が投げる例外（``infrastructure/research/errors.py``）と同じ型を使う
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

from domain.research.ports import (
    FetchedContent,
    FetchErrorKind,
    SearchHit,
    SearchQuery,
    SearchResults,
    StatObservation,
)
from infrastructure.research.errors import ProviderRateLimited
from infrastructure.research.fake_corpus import (
    OBSERVED_AT,
    CorpusDocument,
    ResearchCorpus,
    default_corpus,
)
from infrastructure.research.quota_costs import YOUTUBE_FULL_SEARCH_UNITS

#: Fake の「現在時刻」（固定）。時計を注入すれば変えられる
FIXED_NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)


class _FaultInjection:
    def __init__(self) -> None:
        self._pending: list[BaseException] = []

    def fail_next(self, error: BaseException, times: int = 1) -> None:
        """次の ``times`` 回の呼び出しで ``error`` を投げる。"""
        self._pending.extend([error] * times)

    def _maybe_fail(self) -> None:
        if self._pending:
            raise self._pending.pop(0)


class FakeSearchProvider(_FaultInjection):
    """``SearchProvider``。``kind`` が web / youtube のどちらの query も扱う。"""

    name = "fake"

    def __init__(
        self,
        corpus: ResearchCorpus | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        youtube_cost_units: int = YOUTUBE_FULL_SEARCH_UNITS,
    ) -> None:
        super().__init__()
        self._corpus = corpus or default_corpus()
        self._clock = clock or (lambda: FIXED_NOW)
        self._youtube_cost_units = youtube_cost_units
        self.calls: list[SearchQuery] = []

    def fail_with_429(self, times: int = 1) -> None:
        self.fail_next(ProviderRateLimited("fake provider: rate limited (429)"), times)

    async def search(self, query: SearchQuery) -> SearchResults:
        if query.max_results < 1:
            raise ValueError("max_results must be >= 1")
        self.calls.append(query)  # 失敗した呼び出しも「呼んだ」履歴に残す
        self._maybe_fail()
        matched = self._corpus.search(query)
        hits = tuple(self._hit(doc, query) for doc in matched[: query.max_results])
        return SearchResults(
            query=query,
            hits=hits,
            searched_at=self._clock(),
            provider=self.name,
            cost_units=self._youtube_cost_units if query.kind == "youtube" else 0,
            truncated=len(matched) > query.max_results,
        )

    @staticmethod
    def _hit(doc: CorpusDocument, query: SearchQuery) -> SearchHit:
        extras: dict[str, str] = {}
        # 指定した値を残すだけ。人気・言語・視聴者層を断定しない
        if query.region_code:
            extras["requested_region_code"] = query.region_code
        if query.language:
            extras["requested_relevance_language"] = query.language
        stats = tuple(
            StatObservation(metric, value, "count", OBSERVED_AT)
            for metric, value in (
                ("view_count", doc.view_count),
                ("like_count", doc.like_count),
                ("comment_count", doc.comment_count),
            )
            if value is not None
        )
        return SearchHit(
            url=doc.url,
            title=doc.title,
            snippet=doc.snippet,
            published_at=doc.published_at,
            provider="fake",
            provider_ref=doc.video_id,
            channel_id=doc.channel_id,
            channel_title=doc.channel_title,
            stats=stats,
            channel_subscriber_count=doc.subscriber_count,
            duration_seconds=doc.duration_seconds,
            extras=extras,
        )


class FakeContentFetcher(_FaultInjection):
    """``ContentFetcher``。URL → 本文 / 失敗はコーパスで決まる。未知の URL は 404。"""

    def __init__(
        self,
        corpus: ResearchCorpus | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        self._corpus = corpus or default_corpus()
        self._clock = clock or (lambda: FIXED_NOW)
        self.calls: list[str] = []
        self._forced: list[tuple[FetchErrorKind, int | None]] = []

    def fail_next_with(self, kind: FetchErrorKind, status_code: int | None = None) -> None:
        """次の取得を、例外ではなく ``failed`` の値（本物の fetcher と同じ形）で失敗させる。"""
        self._forced.append((kind, status_code))

    async def fetch(self, url: str) -> FetchedContent:
        self.calls.append(url)
        self._maybe_fail()
        if self._forced:
            kind, status = self._forced.pop(0)
            return self._failed(url, url, (), kind, status)
        doc = self._corpus.by_url(url)
        chain: tuple[str, ...] = ()
        final = url
        if doc is not None and doc.redirect_to is not None:
            chain, final = (url,), doc.redirect_to
            doc = self._corpus.by_url(final)
        if doc is None:
            return self._failed(url, final, chain, "http_4xx", 404)
        if doc.fetch_failure is not None:
            return self._failed(url, final, chain, doc.fetch_failure, doc.fetch_status_code)
        if doc.body is None:
            return self._failed(url, final, chain, "http_4xx", 404)
        data = doc.body.encode("utf-8")
        return FetchedContent(
            requested_url=url,
            final_url=final,
            redirect_chain=chain,
            status_code=200,
            content_type=doc.content_type,
            text=doc.body,
            content_sha256=hashlib.sha256(data).hexdigest(),
            bytes_read=len(data),
            truncated=doc.truncated,
            fetched_at=self._clock(),
            fetch_status="truncated" if doc.truncated else "fetched",
            error="too_large" if doc.truncated else None,
            error_detail="stream_exceeded" if doc.truncated else None,
        )

    def _failed(
        self,
        requested: str,
        final: str,
        chain: tuple[str, ...],
        kind: FetchErrorKind,
        status: int | None,
    ) -> FetchedContent:
        return FetchedContent(
            requested_url=requested,
            final_url=final,
            redirect_chain=chain,
            status_code=status,
            content_type=None,
            text=None,
            content_sha256=None,
            bytes_read=0,
            truncated=False,
            fetched_at=self._clock(),
            fetch_status="failed",
            error=kind,
            error_detail="fake",
        )
