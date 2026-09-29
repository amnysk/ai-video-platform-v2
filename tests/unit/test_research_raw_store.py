"""外部呼び出しの生データの保存（ADR-0037 §8）。再実行はこれを読み、同じ呼び出しを送り直さない。

理由は docs/testing/research-execution-rationale.md。
"""

from __future__ import annotations

import pytest

from domain.errors import ArtifactConflictError
from domain.research.ports import SearchQuery
from infrastructure.research.fake_corpus import MEIJI_A, TIMEOUT_URL
from infrastructure.research.fake_providers import FakeContentFetcher, FakeSearchProvider
from infrastructure.research.raw_store import FETCH_CODEC, SEARCH_CODEC, ResearchRawStore

REQUEST_ID = "7d1c7a53-3a8e-5e0b-9e53-1c2b3d4e5f60"
CALL_ID = "0b6a3c55-1d2e-4f70-8a9b-0c1d2e3f4a5b"


async def test_search_results_round_trip_exactly(artifact_store) -> None:
    results = await FakeSearchProvider().search(
        SearchQuery(text="関ヶ原", kind="youtube", max_results=2, region_code="JP")
    )
    raw = ResearchRawStore(artifact_store)
    assert await raw.find(SEARCH_CODEC, REQUEST_ID, CALL_ID) is None
    key = await raw.save(SEARCH_CODEC, REQUEST_ID, CALL_ID, results)
    assert key == f"research/{REQUEST_ID}/raw/search/{CALL_ID}.json"
    assert await raw.find(SEARCH_CODEC, REQUEST_ID, CALL_ID) == results


@pytest.mark.parametrize("url", [MEIJI_A, TIMEOUT_URL])
async def test_fetched_content_round_trips_including_failures(artifact_store, url: str) -> None:
    content = await FakeContentFetcher().fetch(url)
    raw = ResearchRawStore(artifact_store)
    await raw.save(FETCH_CODEC, REQUEST_ID, CALL_ID, content)
    assert await raw.find(FETCH_CODEC, REQUEST_ID, CALL_ID) == content


async def test_the_evidence_of_a_call_cannot_be_rewritten(artifact_store) -> None:
    raw = ResearchRawStore(artifact_store)
    fetcher = FakeContentFetcher()
    await raw.save(FETCH_CODEC, REQUEST_ID, CALL_ID, await fetcher.fetch(MEIJI_A))
    with pytest.raises(ArtifactConflictError):
        await raw.save(FETCH_CODEC, REQUEST_ID, CALL_ID, await fetcher.fetch(TIMEOUT_URL))
