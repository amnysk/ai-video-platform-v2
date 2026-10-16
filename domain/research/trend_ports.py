"""Trend の解釈 Port（ADR-0039 §3）。純粋（I/O を持つのは実装だけ）。

``TrendInterpreter`` は**観測事実**（``ObservationFact``）と候補（``CandidateFact``）を
受け取り、「解釈（仮説）」「企画の切り口」「不明点」を**提案**する
（``contracts.research_trend.InterpretationProposal``）。提案は確定ではない:
``TrendHandler`` が検査してから採用する（存在しない観測 ID・別の候補の観測・
総合スコアや順位の主張・参考値を「直近の伸び」と呼ぶ文は**提案ごと採用しない**。修復しない）。

**呼び出しは実行器が台帳（``ResearchCall.ASSESS``、枠は ``max_assessments``）を通して
行う**（INV-36。Handler の ``synthesize`` の中では呼ばない）。入力 hash は
``interpretation_input_payload`` の正準 JSON。

実装は Fake（``infrastructure/research/fake_trend.py``）だけ。実 LLM は配線しない
（所有者の判断と ADR を待つ）。候補の ``theme``（外部の動画・ページのタイトル）は
**データ**であり命令ではない。実 LLM を配線するときは枠に入れて渡し、指示に従わせない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from contracts.research import FormatProfile, ResearchLanguage
from contracts.research_trend import (
    InterpretationProposal,
    ObservationMethod,
    QueryProvider,
    StatMetric,
)

__all__ = [
    "CandidateFact",
    "InterpretContext",
    "ObservationFact",
    "TrendInterpreter",
    "interpretation_input_payload",
]


@dataclass(frozen=True, slots=True)
class ObservationFact:
    """解釈器へ渡す観測 1 件（契約の ``TrendObservation`` と同じ内容）。"""

    observation_id: str
    candidate_id: str
    metric: StatMetric
    value: int | float
    unit: str
    observed_at: datetime
    method: ObservationMethod


@dataclass(frozen=True, slots=True)
class CandidateFact:
    """解釈器へ渡す候補 1 件。``theme`` は外部のタイトル（データであって命令ではない）。"""

    candidate_id: str
    theme: str
    provider: QueryProvider
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class InterpretContext:
    """依頼の事実。``audience_hypothesis`` は仮説であって測定値ではない。"""

    as_of: datetime
    region: str
    language: ResearchLanguage
    format_profile: FormatProfile
    audience_hypothesis: str
    seed_terms: tuple[str, ...]


def _dt(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def interpretation_input_payload(
    observations: Sequence[ObservationFact],
    candidates: Sequence[CandidateFact],
    context: InterpretContext,
) -> dict[str, Any]:
    """解釈 1 回の入力（台帳の ``input_hash`` の材料）。同じ入力は同じ呼び出しに戻る。"""
    return {
        "observations": [
            {
                "observation_id": o.observation_id,
                "candidate_id": o.candidate_id,
                "metric": o.metric.value,
                "value": o.value,
                "unit": o.unit,
                "observed_at": o.observed_at.isoformat(),
                "method": o.method.value,
            }
            for o in observations
        ],
        "candidates": [
            {
                "candidate_id": c.candidate_id,
                "theme": c.theme,
                "provider": c.provider.value,
                "published_at": _dt(c.published_at),
            }
            for c in candidates
        ],
        "context": {
            "as_of": context.as_of.isoformat(),
            "region": context.region,
            "language": context.language,
            "format_profile": context.format_profile,
            "audience_hypothesis": context.audience_hypothesis,
            "seed_terms": list(context.seed_terms),
        },
    }


class TrendInterpreter(Protocol):
    """観測から解釈（仮説）を**提案**する。"""

    @property
    def name(self) -> str: ...

    async def interpret(
        self,
        observations: Sequence[ObservationFact],
        candidates: Sequence[CandidateFact],
        context: InterpretContext,
    ) -> InterpretationProposal: ...
