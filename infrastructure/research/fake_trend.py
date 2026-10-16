"""``TrendInterpreter`` の Fake（ADR-0039）。通常テストと ``RESEARCH_PROVIDER=fake`` の唯一の実装。

実ネットワーク・実 API・時計・乱数に依存しない（INV-18）。観測から単純な仮説を組むだけで、
意味を理解しない。**わざと悪い提案**（``BadInterpretation``）を返すモードを持ち、コード側
（実行器の schema 検査と ``trend_handler.adopt_interpretation``）がそれを採用しないことを
テストで示す。悪い提案は実 LLM の出力が壊れる形を再現する:

- ``unknown_observation_id``: 存在しない観測 ID を根拠にする
- ``other_candidate_observation``: 別の候補の観測を根拠にする
- ``score_in_text``: 文章の中で総合スコア・順位を主張する
- ``recent_growth_claim``: 公開からの平均（参考値）を「直近の伸び」と言う
- ``unknown_angle_key``: 存在しない解釈を根拠にした切り口
- ``overall_score`` / ``fabricated_observation``: 型に無い欄（総合スコア・観測の捏造）を持つ
  （schema 違反。実行器が ``ResearchOutputInvalidError`` として採用しない）
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict

from contracts.research_trend import (
    InterpretationProposal,
    ObservationMethod,
    ProposedAngle,
    ProposedInterpretation,
    StatMetric,
)
from domain.research.trend_ports import CandidateFact, InterpretContext, ObservationFact

__all__ = ["MAX_FAKE_INTERPRETATIONS", "BadInterpretation", "FakeTrendInterpreter"]

#: 良い提案で解釈する候補の上限（決定的な単純さのため）
MAX_FAKE_INTERPRETATIONS = 3

_GROWTH = (StatMetric.VIEWS_PER_HOUR_DELTA, StatMetric.LIFETIME_AVERAGE_VIEWS_PER_HOUR)


class BadInterpretation(StrEnum):
    UNKNOWN_OBSERVATION_ID = "unknown_observation_id"
    OTHER_CANDIDATE_OBSERVATION = "other_candidate_observation"
    SCORE_IN_TEXT = "score_in_text"
    RECENT_GROWTH_CLAIM = "recent_growth_claim"
    UNKNOWN_ANGLE_KEY = "unknown_angle_key"
    OVERALL_SCORE = "overall_score"
    FABRICATED_OBSERVATION = "fabricated_observation"


class _ExtraFieldsProposal(InterpretationProposal):
    """壊れた LLM 出力の再現: 型に無い欄を持つ（``extra=allow`` にして構築だけ通す）。"""

    model_config = ConfigDict(extra="allow", frozen=True)


class FakeTrendInterpreter:
    name = "fake"

    def __init__(self, *, bad: BadInterpretation | None = None) -> None:
        self._bad = bad
        self.calls: list[tuple[tuple[ObservationFact, ...], tuple[CandidateFact, ...]]] = []
        self._pending: list[BaseException] = []

    def fail_next(self, error: BaseException, times: int = 1) -> None:
        self._pending.extend([error] * times)

    async def interpret(
        self,
        observations: Sequence[ObservationFact],
        candidates: Sequence[CandidateFact],
        context: InterpretContext,
    ) -> InterpretationProposal:
        del context
        self.calls.append((tuple(observations), tuple(candidates)))
        if self._pending:
            raise self._pending.pop(0)
        interpretations = _good_interpretations(observations, candidates)
        proposal = InterpretationProposal(
            interpretations=interpretations,
            angles=_good_angles(interpretations, candidates),
            unknowns=("視聴者の地域・年齢は観測できていない",),
        )
        if self._bad is None or not observations:
            return proposal
        return _bad(self._bad, proposal, observations)


def _good_interpretations(
    observations: Sequence[ObservationFact], candidates: Sequence[CandidateFact]
) -> tuple[ProposedInterpretation, ...]:
    out: list[ProposedInterpretation] = []
    for candidate in candidates:
        own = [o for o in observations if o.candidate_id == candidate.candidate_id]
        growth = next((o for o in own if o.metric in _GROWTH), None)
        views = next((o for o in own if o.metric is StatMetric.VIEWS_TOTAL), None)
        basis = growth or views
        if basis is None:
            continue
        kind = (
            "公開後の平均の再生速度（参考値）"
            if basis.method is ObservationMethod.LIFETIME_AVERAGE
            else "観測間の再生速度の差分"
            if basis.method is ObservationMethod.DELTA
            else "累積再生数"
        )
        out.append(
            ProposedInterpretation(
                key=f"h{len(out) + 1}",
                candidate_id=candidate.candidate_id,
                text=(
                    f"「{candidate.theme[:60]}」は{kind}が観測されており、"
                    "この題材に関心がある可能性がある（仮説）"
                ),
                basis_observation_ids=(basis.observation_id,),
            )
        )
        if len(out) >= MAX_FAKE_INTERPRETATIONS:
            break
    return tuple(out)


def _good_angles(
    interpretations: Sequence[ProposedInterpretation], candidates: Sequence[CandidateFact]
) -> tuple[ProposedAngle, ...]:
    themes = {c.candidate_id: c.theme for c in candidates}
    return tuple(
        ProposedAngle(
            candidate_id=i.candidate_id,
            text=f"「{themes.get(i.candidate_id or '', '')[:60]}」の背景を別の視点で試す",
            basis_interpretation_keys=(i.key,),
        )
        for i in interpretations
    )


def _bad(
    mode: BadInterpretation,
    good: InterpretationProposal,
    observations: Sequence[ObservationFact],
) -> InterpretationProposal:
    first = observations[0]

    def one(**changes: Any) -> InterpretationProposal:
        values: dict[str, Any] = {
            "key": "h1",
            "candidate_id": first.candidate_id,
            "text": "この題材に関心がある可能性がある（仮説）",
            "basis_observation_ids": (first.observation_id,),
        }
        values.update(changes)
        return InterpretationProposal(
            interpretations=(ProposedInterpretation(**values),), angles=(), unknowns=()
        )

    if mode is BadInterpretation.UNKNOWN_OBSERVATION_ID:
        return one(basis_observation_ids=("O-999",))
    if mode is BadInterpretation.OTHER_CANDIDATE_OBSERVATION:
        other = next((o for o in observations if o.candidate_id != first.candidate_id), None)
        return one(basis_observation_ids=(other.observation_id if other else "O-999",))
    if mode is BadInterpretation.SCORE_IN_TEXT:
        return one(text="総合スコア 87 の有望テーマ")
    if mode is BadInterpretation.RECENT_GROWTH_CLAIM:
        lifetime = next(
            (o for o in observations if o.method is ObservationMethod.LIFETIME_AVERAGE), first
        )
        return one(
            candidate_id=lifetime.candidate_id,
            text="直近の伸びが大きい題材",
            basis_observation_ids=(lifetime.observation_id,),
        )
    if mode is BadInterpretation.UNKNOWN_ANGLE_KEY:
        keyed = one()
        return InterpretationProposal(
            interpretations=keyed.interpretations,
            angles=(
                ProposedAngle(
                    candidate_id=None, text="根拠の無い切り口", basis_interpretation_keys=("nope",)
                ),
            ),
            unknowns=(),
        )
    extra: dict[str, Any] = (
        {"overall_score": 87}
        if mode is BadInterpretation.OVERALL_SCORE
        else {"observations": [{"metric": "views_total", "value": 9_999_999, "unit": "views"}]}
    )
    return _ExtraFieldsProposal.model_validate({**good.model_dump(), **extra})
