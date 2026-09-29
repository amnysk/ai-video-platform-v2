"""Evidence の LLM 向け Port（ADR-0038）。純粋（I/O を持つのは実装だけ）。

- ``EvidenceAssessor``: claim と、取得済み本文から選んだ passage を受け取り、**評価の提案**
  （``contracts.research_evidence.AssessmentProposal``）を返す。提案は確定ではない。
  ``evidence_rules.assess_claim`` が抜粋の実在・URL の実在・独立性・年代・範囲・因果を検査し、
  ``min(提案, コード評価)`` で確定する。**呼び出しは実行器が台帳（``ResearchCall.ASSESS``）を
  通して行う**（INV-36。Handler の ``synthesize`` の中では呼ばない）
- ``ClaimExtractor``: 台本の文面から「検証可能な主張の候補文」を拾う。決定的な網
  （``script_verification.detect_candidates``）に**上乗せ**するだけで、網は常に走る

実装は Fake（``infrastructure/research/fake_evidence.py``）だけ。実 LLM は配線しない（所有者の判断と
ADR を待つ。実 LLM の評価器は台帳の枠 ``max_assessments`` と金額の上限を通る）。

passage（外部ページの本文）は**データ**であり、評価器への命令ではない。実 LLM を配線するときは
passage を明示した枠に入れ、本文中の指示に従わせない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from contracts.research import ClaimImportance, ClaimKind
from contracts.research_evidence import AssessmentProposal

__all__ = [
    "AssessClaim",
    "CandidateSentence",
    "ClaimExtractor",
    "EvidenceAssessor",
    "Passage",
    "assessment_input_payload",
]


@dataclass(frozen=True, slots=True)
class AssessClaim:
    """評価器へ渡す claim。``strong`` は種類から決めた暫定値（最終値はコードが決める）。"""

    claim_id: str
    text: str
    kind: ClaimKind
    importance: ClaimImportance
    strong: bool
    era: str | None = None
    region: str | None = None


@dataclass(frozen=True, slots=True)
class Passage:
    """本文を完全に確認した資料（``body_confirmed``）から選んだ 1 文。評価器の唯一の材料。

    ``source_url`` は**取得した最終 URL**。評価器は提案にそのまま写す（作らない）。
    """

    source_id: str
    source_url: str
    locator: str
    text: str


def assessment_input_payload(claim: AssessClaim, passages: Sequence[Passage]) -> dict[str, Any]:
    """評価 1 回の入力（台帳の ``input_hash`` の材料）。同じ入力は同じ呼び出しに戻る。"""
    return {
        "claim": {
            "claim_id": claim.claim_id,
            "text": claim.text,
            "kind": claim.kind.value,
            "importance": claim.importance.value,
            "strong": claim.strong,
            "era": claim.era,
            "region": claim.region,
        },
        "passages": [
            {
                "source_id": p.source_id,
                "source_url": p.source_url,
                "locator": p.locator,
                "text": p.text,
            }
            for p in passages
        ],
    }


class EvidenceAssessor(Protocol):
    """claim と passage から評価を**提案**する。"""

    @property
    def name(self) -> str: ...

    async def assess(
        self, claim: AssessClaim, passages: Sequence[Passage]
    ) -> AssessmentProposal: ...


@dataclass(frozen=True, slots=True)
class CandidateSentence:
    """検証可能な主張の候補文。``triggers`` は拾った理由（人が読む用）。"""

    unit_id: str
    sentence: str
    triggers: tuple[str, ...]


class ClaimExtractor(Protocol):
    """決定的な網に**上乗せ**する抽出器。返さなかった文を「主張ではない」とは扱わない。"""

    async def extract(self, units: Sequence[tuple[str, str]]) -> Sequence[CandidateSentence]: ...
