"""Evidence の Fake の LLM Port（ADR-0038）。通常テストと ``RESEARCH_PROVIDER=fake`` の唯一の実装。

実ネットワーク・実 API・時計・乱数に依存しない（INV-18）。

- ``FakeEvidenceAssessor``: 意味を理解しない規則ベースの評価器。claim のキーワードを十分含む passage
  を ``supports``（異説の語を含めば ``refutes``）として提案する。**わざと悪い提案**
  （``BadProposal``）を返すモードを持ち、``evidence_rules`` がそれを拒否・弱めることを示す
- ``FakeClaimExtractor``: ``needles`` の語を含む文を候補として返す（決定的な網の上乗せの検査用）
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from contracts.research_evidence import AssessmentProposal, ClaimAssessment, ProposedLink, Stance
from domain.research.evidence_ports import AssessClaim, CandidateSentence, Passage
from domain.research.evidence_text import (
    detect_language,
    extract_keywords,
    has_dispute_terms,
    keyword_hits,
)
from domain.research.script_verification import TRIGGER_EXTRACTOR, split_sentences

__all__ = ["MADE_UP_URL", "BadProposal", "FakeClaimExtractor", "FakeEvidenceAssessor"]

#: ``UNFETCHED_URL`` が使う、取得していない URL
MADE_UP_URL = "https://made-up.example/never-fetched"
_MADE_UP_EXCERPT = {
    "ja": "この文はどの取得済み本文にも存在しない。",
    "en": "No fetched body says this.",
}


class BadProposal(StrEnum):
    """評価器が返しうる悪い提案。すべて ``supported`` の強すぎる評価で返す。"""

    FABRICATED_EXCERPT = "fabricated_excerpt"
    UNFETCHED_URL = "unfetched_url"
    UNKNOWN_SOURCE_ID = "unknown_source_id"
    #: 全 passage を無条件に ``supports`` にし、資料より強い表現（全称・因果）を添える
    OVERSTATED = "overstated"


class FakeEvidenceAssessor:
    name = "fake"

    def __init__(self, *, bad: BadProposal | None = None, min_keyword_ratio: float = 0.5) -> None:
        self._bad = bad
        self._min_ratio = min_keyword_ratio
        self.calls: list[tuple[AssessClaim, tuple[Passage, ...]]] = []
        self._pending: list[BaseException] = []

    def fail_next(self, error: BaseException, times: int = 1) -> None:
        self._pending.extend([error] * times)

    async def assess(self, claim: AssessClaim, passages: Sequence[Passage]) -> AssessmentProposal:
        self.calls.append((claim, tuple(passages)))
        if self._pending:
            raise self._pending.pop(0)
        if self._bad is not None and passages:
            return self._bad_proposal(claim, passages, self._bad)
        return self._honest(claim, passages)

    def _honest(self, claim: AssessClaim, passages: Sequence[Passage]) -> AssessmentProposal:
        keywords = extract_keywords(claim.text)
        links: list[ProposedLink] = []
        seen: set[tuple[str, Stance]] = set()
        for passage in passages:
            if (
                not keywords
                or keyword_hits(keywords, passage.text) / len(keywords) < self._min_ratio
            ):
                continue
            stance = Stance.REFUTES if has_dispute_terms(passage.text) else Stance.SUPPORTS
            if (passage.source_id, stance) in seen:
                continue
            seen.add((passage.source_id, stance))
            links.append(_link(passage, stance, passage.text))
        supports = [link for link in links if link.stance is Stance.SUPPORTS]
        refutes = [link for link in links if link.stance is Stance.REFUTES]
        if supports and refutes:
            assessment = ClaimAssessment.DISPUTED
        elif supports:
            assessment = ClaimAssessment.SUPPORTED
        else:
            assessment = ClaimAssessment.INSUFFICIENT
        return AssessmentProposal(
            claim_id=claim.claim_id,
            assessment=assessment,
            links=tuple(links[:8]),
            usable_expression=supports[0].excerpt if supports else "",
            reason=f"fake: {len(supports)} supporting and {len(refutes)} disputing passage(s)",
            unverified_points=(),
        )

    def _bad_proposal(
        self, claim: AssessClaim, passages: Sequence[Passage], mode: BadProposal
    ) -> AssessmentProposal:
        first = passages[0]
        lang = detect_language(claim.text)
        if mode is BadProposal.FABRICATED_EXCERPT:
            links = [_link(first, Stance.SUPPORTS, _MADE_UP_EXCERPT[lang])]
        elif mode is BadProposal.UNFETCHED_URL:
            links = [
                ProposedLink(
                    source_id=first.source_id,
                    source_url=MADE_UP_URL,
                    stance=Stance.SUPPORTS,
                    locator=first.locator,
                    excerpt=first.text,
                )
            ]
        elif mode is BadProposal.UNKNOWN_SOURCE_ID:
            links = [
                ProposedLink(
                    source_id="S-999",
                    source_url=first.source_url,
                    stance=Stance.SUPPORTS,
                    locator=first.locator,
                    excerpt=first.text,
                )
            ]
        else:
            distinct = {p.source_id: p for p in reversed(passages)}
            links = [_link(p, Stance.SUPPORTS, p.text) for p in distinct.values()]
        overstated = (
            f"{first.text} これは常にすべてに当てはまり、そのために起きた。"
            if lang == "ja"
            else f"{first.text} This was always true of every case, because of it."
        )
        return AssessmentProposal(
            claim_id=claim.claim_id,
            assessment=ClaimAssessment.SUPPORTED,
            links=tuple(links[:8]),
            usable_expression=overstated[:400],
            reason=f"fake ({mode.value}): confidently supported",
            unverified_points=(),
        )


def _link(passage: Passage, stance: Stance, excerpt: str) -> ProposedLink:
    return ProposedLink(
        source_id=passage.source_id,
        source_url=passage.source_url,
        stance=stance,
        locator=passage.locator,
        excerpt=excerpt[:300],
    )


class FakeClaimExtractor:
    """``needles`` の語を含む文を候補として返す。何も挙げなければ常に空。"""

    def __init__(self, needles: Sequence[str] = ()) -> None:
        self._needles = tuple(needles)
        self.calls: list[tuple[str, ...]] = []

    async def extract(self, units: Sequence[tuple[str, str]]) -> list[CandidateSentence]:
        self.calls.append(tuple(unit_id for unit_id, _ in units))
        return [
            CandidateSentence(unit_id, sentence, (TRIGGER_EXTRACTOR,))
            for unit_id, text in units
            for sentence in split_sentences(text)
            if any(needle in sentence for needle in self._needles)
        ]
