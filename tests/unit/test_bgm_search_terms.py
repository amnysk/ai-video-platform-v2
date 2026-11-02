"""検索語の生成（決定的・上限つき・自由入力を Commons の検索構文へ流さない）。"""

from __future__ import annotations

from contracts.music_sources import ACQUISITION_MAX_SEARCHES_PER_RUN
from domain.music.search_terms import build_search_queries

REQ = {"mood": "calm", "culture": "japanese", "energy": "low", "tempo_band": "slow"}


def test_deterministic_and_bounded() -> None:
    a = build_search_queries(REQ)
    assert a == build_search_queries(dict(reversed(list(REQ.items()))))
    assert 1 <= len(a) <= ACQUISITION_MAX_SEARCHES_PER_RUN
    assert len(set(a)) == len(a)


def test_queries_are_audio_cc0_scoped() -> None:
    for q in build_search_queries(REQ):
        assert q.startswith('filetype:audio incategory:"CC-Zero"')


def test_requirements_influence_the_queries() -> None:
    q = " ".join(build_search_queries(REQ))
    assert "ambient" in q and "shakuhachi" in q
    other = " ".join(build_search_queries({**REQ, "mood": "tense", "culture": "western"}))
    assert other != q


def test_unknown_or_hostile_values_never_reach_the_query() -> None:
    qs = build_search_queries({"mood": 'x" insource:evil -incategory:"CC-Zero', "culture": "??"})
    assert qs
    for q in qs:
        assert "evil" not in q and "insource" not in q
        assert q.count('"') == 2


def test_empty_requirements_still_give_a_generic_query() -> None:
    assert build_search_queries({}) == ['filetype:audio incategory:"CC-Zero" music']
