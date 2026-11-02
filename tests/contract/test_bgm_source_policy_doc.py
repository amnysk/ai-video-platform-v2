"""ソース方針の表（``contracts.music_sources``）と規約調査の文書が食い違わない（ADR-0087 §1）。"""

from __future__ import annotations

import pathlib

from contracts.music_sources import SOURCE_POLICIES, MusicSourceId

DOC = pathlib.Path(__file__).resolve().parents[2] / "docs/research/bgm-sources-2026-11.md"

NAMES = {
    MusicSourceId.WIKIMEDIA_COMMONS: "Wikimedia Commons",
    MusicSourceId.FREE_MUSIC_ARCHIVE: "Free Music Archive",
    MusicSourceId.PIXABAY_MUSIC: "Pixabay",
    MusicSourceId.CCMIXTER: "ccMixter",
    MusicSourceId.INCOMPETECH: "incompetech",
}


def test_every_searchable_source_is_covered_by_the_research_doc() -> None:
    text = DOC.read_text(encoding="utf-8")
    for source, policy in SOURCE_POLICIES.items():
        if not policy.search_allowed:
            continue
        assert NAMES[source] in text, source
        assert policy.terms_checked_on in text, (source, policy.terms_checked_on)


def test_only_commons_is_auto_acquirable_and_the_doc_says_so() -> None:
    auto = [s for s, p in SOURCE_POLICIES.items() if p.auto_acquire_allowed]
    assert auto == [MusicSourceId.WIKIMEDIA_COMMONS]
    text = DOC.read_text(encoding="utf-8")
    assert "CC0 のみ自動承認" in text
    assert "https://www.mediawiki.org/wiki/API:Etiquette" in text
    assert "User-Agent" in text
