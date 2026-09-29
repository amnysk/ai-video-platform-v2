"""Web 検索 Provider は選定未了（ADR-0036 §3）。

費用・quota・利用条件が未確認のため、推測で外部サービスへ固定しない。所有者が選び ADR を足すまで、
呼ぶと ``ResearchProviderNotConfigured``（domain では ``ResearchProviderNotConfiguredError``、
``needs_input`` = 依頼は ``blocked``）。**実ネットワークに出ない。**
"""

from __future__ import annotations

from domain.research.ports import SearchQuery, SearchResults
from infrastructure.research.errors import ResearchProviderNotConfigured


class NotConfiguredSearchProvider:
    name = "web"

    async def search(self, query: SearchQuery) -> SearchResults:
        raise ResearchProviderNotConfigured(
            "no web search provider is configured; the owner must choose one (ADR-0036 §3)"
        )
