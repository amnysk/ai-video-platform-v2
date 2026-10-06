"""Trend が使う Tier A の Fake の拡張（ADR-0039 §7）。

- ``FakeSearchProvider(stat_observed_at=)``: 統計の観測時刻を変えられる（既定は従来の固定値のまま）
- ``fake_corpus.with_view_counts``: 同じ動画の別の時点の再生数を作る（元のコーパスは変えない）
- コーパスの YouTube の channel id は本物の形（``YOUTUBE_CHANNEL_ID_PATTERN``）。形の違う id は
  Trend の契約が参照から落とすので、Fake が本物と違う形だと Trend の検査が空振りする

理由は docs/testing/research-trend-rationale.md。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from contracts.upload import YOUTUBE_CHANNEL_ID_PATTERN
from domain.research.ports import SearchQuery
from infrastructure.research.fake_corpus import (
    OBSERVED_AT,
    YT_SEKIGAHARA_SHORT,
    default_corpus,
    with_view_counts,
)
from infrastructure.research.fake_providers import FakeSearchProvider

QUERY = SearchQuery(text="関ヶ原", kind="youtube", max_results=5)


def _views(results, url: str) -> tuple[int | float, datetime]:
    hit = next(h for h in results.hits if h.url == url)
    stat = next(s for s in hit.stats if s.metric == "view_count")
    return stat.value, stat.observed_at


async def test_the_default_observation_time_is_unchanged() -> None:
    results = await FakeSearchProvider().search(QUERY)
    assert all(s.observed_at == OBSERVED_AT for h in results.hits for s in h.stats)


async def test_stat_observed_at_and_view_counts_make_a_second_observation() -> None:
    later = datetime(2026, 9, 25, tzinfo=UTC)
    corpus = with_view_counts(default_corpus(), {YT_SEKIGAHARA_SHORT: 1_000_000})
    first = await FakeSearchProvider().search(QUERY)
    second = await FakeSearchProvider(corpus, stat_observed_at=later).search(QUERY)
    assert _views(first, YT_SEKIGAHARA_SHORT) == (980_000, OBSERVED_AT)
    assert _views(second, YT_SEKIGAHARA_SHORT) == (1_000_000, later)
    # 元のコーパスは変わらない
    assert _views(await FakeSearchProvider().search(QUERY), YT_SEKIGAHARA_SHORT)[0] == 980_000


def test_corpus_channel_ids_have_the_real_youtube_shape() -> None:
    ids = [d.channel_id for d in default_corpus().documents if d.kind == "youtube"]
    assert ids and all(
        cid is not None and re.fullmatch(YOUTUBE_CHANNEL_ID_PATTERN, cid) for cid in ids
    )
