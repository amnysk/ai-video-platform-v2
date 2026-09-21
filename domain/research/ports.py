"""research の Provider Port（ADR-0031 §7）。純粋。I/O・HTTP・DNS を持たない（INV-6）。

Trend / Evidence は**同じ Port** を使う。実装は ``infrastructure/research/``
（``FakeSearchProvider`` / ``HttpContentFetcher`` / ``NotConfiguredSearchProvider``）と
``infrastructure/youtube/search.py``。

外部ページの本文は**データ**であり命令ではない。この Port は本文をそのまま返すだけで、
解釈（LLM への受け渡し・主張の抽出）は別の層が枠に入れて行う。
``FetchedContent.body_confirmed`` が真でなければ、その資料の本文を確認したことにしてはならない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

SearchKind = Literal["web", "youtube"]

FetchStatus = Literal["fetched", "truncated", "failed"]

#: 取得失敗の種別（``FetchStatus == "failed"``、または切り詰めの理由）。例外ではなく値で返す。
#: 429 は ``http_4xx`` + ``status_code == 429``（再試行できる）。分類は
#: ``infrastructure/research/errors.py`` の ``to_domain_error``。
FetchErrorKind = Literal[
    "blocked_url",
    "timeout",
    "too_large",
    "bad_content_type",
    "http_4xx",
    "http_5xx",
    "network",
]


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """検索の依頼。``region_code`` / ``language`` は**絞り込み・重みづけ**であって断定ではない。"""

    text: str
    kind: SearchKind
    max_results: int
    region_code: str | None = None
    language: str | None = None
    published_after: datetime | None = None
    published_before: datetime | None = None


@dataclass(frozen=True, slots=True)
class StatObservation:
    """ある時点で観測した数値。``observed_at`` を持たない統計は Port を通らない。"""

    metric: str
    value: int | float
    unit: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SearchHit:
    """検索結果 1 件。snippet は本文ではない（本文確認は ``ContentFetcher`` の結果で判定する）。"""

    url: str
    title: str
    snippet: str
    published_at: datetime | None
    provider: str
    provider_ref: str | None = None
    channel_id: str | None = None
    channel_title: str | None = None
    stats: tuple[StatObservation, ...] = ()
    channel_subscriber_count: int | None = None
    duration_seconds: int | None = None
    #: 依頼時に**指定した**値など、Provider の断定ではない補足（例: ``requested_region_code``）
    extras: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchResults:
    query: SearchQuery
    hits: tuple[SearchHit, ...]
    searched_at: datetime
    provider: str
    #: YouTube の quota 単位。Web 検索は 0（金額は予約側が設定単価から見積もる）
    cost_units: int
    #: 結果が ``max_results`` などで打ち切られている（続きがある）
    truncated: bool
    #: 一部の補助取得に失敗した等。結果は使えるが完全ではない（例: 購読者数が取れない）
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FetchedContent:
    """本文取得の結果。失敗も例外ではなくこの値で返す。

    - ``final_url``: 実際に取得した URL。Source の URL はこれだけから作る（LLM に作らせない）。
    - ``content_sha256``: 取得した**本文 bytes**（Content-Encoding を展開した後）の sha256。
      本文を読まなかった失敗では ``None``。
    - ``text``: デコード済み・タグ除去済みの本文。テキスト化できない資料（PDF 等）は ``None``。
    """

    requested_url: str
    final_url: str
    redirect_chain: tuple[str, ...]
    status_code: int | None
    content_type: str | None
    text: str | None
    content_sha256: str | None
    bytes_read: int
    truncated: bool
    fetched_at: datetime
    fetch_status: FetchStatus
    error: FetchErrorKind | None = None
    #: 短い理由コード（例: ``too_many_redirects`` / ``blocked_ip``）。URL・secret を含めない
    error_detail: str | None = None

    @property
    def body_confirmed(self) -> bool:
        """本文を**完全に**取得し、空でないテキストとして読めた。

        取得失敗・切り詰め・テキスト化できない資料・空本文は偽。この定義は 1 か所だけ。
        """
        return (
            self.fetch_status == "fetched"
            and not self.truncated
            and self.text is not None
            and bool(self.text.strip())
        )


class SearchProvider(Protocol):
    """検索 Provider。予約台帳の ``ProviderCall`` は呼び出し側が ``query.kind`` から決める。"""

    @property
    def name(self) -> str: ...

    async def search(self, query: SearchQuery) -> SearchResults: ...


class ContentFetcher(Protocol):
    """URL の本文取得。期待される失敗（禁止 URL・timeout・4xx/5xx）は例外にせず値で返す。"""

    async def fetch(self, url: str) -> FetchedContent: ...
