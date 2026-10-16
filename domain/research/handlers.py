"""種別（Trend / Evidence）ごとの差し替え点と、検索・取得の計画の純粋な検査（ADR-0037 §8）。

Research の**共通の骨格**（予約・予算・検索と本文取得の実行・生データの保存・成果物の確定・
状態の更新）は実行器（``infrastructure/research/executor.py``）が持つ。種別ごとの**判断**
（何を検索するか・どれを取得するか・結果から成果物をどう組むか）だけを ``ResearchHandler`` の
背後に置く。Trend と Evidence は同じ検索基盤・同じ Port（``ports.py``）を使う。

- ここは純粋（I/O・時刻・乱数なし。INV-6）
- ``plan_searches`` / ``select_fetches`` / ``synthesize`` は決定的（同じ入力なら同じ出力）。
  実行器は再実行のたびに計画し直し、同じ計画から同じ呼び出しキーを作る（再実行が枠を消費しない）
- 上限は Handler を信用しない: 実行器が ``plan_within_ceiling`` / ``dedupe_fetch_targets`` で切る
- 「根拠が見つからなかった」は例外ではなく成果物の中身（実行状態と評価は別。ADR-0037 §3）

拡張点:
- Evidence の Handler（ADR-0038）と Trend の Handler（ADR-0039）はこの Protocol を実装し、
  ``infrastructure/research/registry.py::build_handlers`` に登録する
- LLM による評価（``ResearchCall.ASSESS``）は、実行器の「評価」の段が同じ台帳を通して行う
  （ADR-0038）。評価を使う Handler は ``AssessingHandler``（``plan_assessments``）も実装し、
  実行器が評価した結果を ``SynthesisContext.assessments`` で受け取る。``synthesize`` の中で
  外部を呼ばない
- Trend の解釈（``TrendInterpreter``。ADR-0039）も同じ ``ResearchCall.ASSESS`` の台帳・枠を通る。
  解釈を使う Handler は ``InterpretingHandler``（``plan_interpretation``）を実装し、結果を
  ``SynthesisContext.interpretations`` で受け取る
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from contracts.research import (
    EvidenceResearchRequest,
    ResearchArtifactType,
    ResearchCoverage,
    ResearchKind,
    TrendResearchRequest,
)
from contracts.research_evidence import AssessmentProposal
from contracts.research_trend import InterpretationProposal
from domain.research.evidence_ports import AssessClaim, Passage
from domain.research.ports import FetchedContent, SearchHit, SearchQuery, SearchResults
from domain.research.trend_ports import CandidateFact, InterpretContext, ObservationFact
from domain.research.urls import normalize_url

__all__ = [
    "STEP_ID_PATTERN",
    "TASK_ID_PATTERN",
    "AssessingHandler",
    "AssessmentOutcome",
    "AssessmentTask",
    "FetchTarget",
    "FetchedSource",
    "HandlerOutput",
    "InterpretationOutcome",
    "InterpretationTask",
    "InterpretingHandler",
    "ResearchHandler",
    "ResearchSpec",
    "SearchRound",
    "SearchStep",
    "SynthesisContext",
    "dedupe_fetch_targets",
    "plan_within_ceiling",
]

ResearchSpec = TrendResearchRequest | EvidenceResearchRequest

#: 検索の段の ID（依頼の中で一意・決定的）。警告・成果物に写るので短い英数字に限る。
STEP_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
#: 評価の ID（依頼の中で一意・決定的。例: claim id ``C-001``）
TASK_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


@dataclass(frozen=True, slots=True)
class SearchStep:
    """実行する検索 1 回。"""

    step_id: str
    query: SearchQuery
    #: この検索の目的（claim の id、``trend:<seed>`` など）。人が読む用
    purpose: str


@dataclass(frozen=True, slots=True)
class SearchRound:
    """検索 1 回の結果。失敗・未実行は ``results=None`` と理由コード（``error``）。"""

    step: SearchStep
    results: SearchResults | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FetchTarget:
    """本文を取得する候補。``step_id`` はその資料を見つけた検索。"""

    hit: SearchHit
    step_id: str


@dataclass(frozen=True, slots=True)
class FetchedSource:
    target: FetchTarget
    content: FetchedContent


@dataclass(frozen=True, slots=True)
class AssessmentTask:
    """評価 1 回（評価器への 1 呼び出し。台帳の ``assess`` 1 行）。"""

    #: 依頼の中で一意・決定的（例: claim id）
    task_id: str
    claim: AssessClaim
    passages: tuple[Passage, ...]


@dataclass(frozen=True, slots=True)
class AssessmentOutcome:
    """評価 1 回の結果。失敗・未実行は ``proposal=None`` と理由コード（``error``）。"""

    task_id: str
    proposal: AssessmentProposal | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class InterpretationTask:
    """Trend の解釈 1 回（解釈器への 1 呼び出し。台帳の ``assess`` 1 行。ADR-0039）。"""

    #: 依頼の中で一意・決定的
    task_id: str
    observations: tuple[ObservationFact, ...]
    candidates: tuple[CandidateFact, ...]
    context: InterpretContext


@dataclass(frozen=True, slots=True)
class InterpretationOutcome:
    """解釈 1 回の結果。失敗・未実行は ``proposal=None`` と理由コード（``error``）。"""

    task_id: str
    proposal: InterpretationProposal | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SynthesisContext:
    """``synthesize`` に渡す実行の事実。判断に使うが、成果物の内容そのものではない。"""

    request_id: str
    #: 調査の基準時刻（依頼の ``as_of``。壁時計ではない）
    as_of: datetime
    #: 上限・期限・停止で**実行しなかった**検索と取得（``partial`` の根拠）
    searches_skipped: tuple[str, ...] = ()
    fetches_skipped: tuple[str, ...] = ()
    #: 取得できなかった範囲の理由コード（人が読む用。URL・secret を含めない）
    failures: tuple[str, ...] = ()
    #: 実行器が台帳を通して行った評価の結果（``AssessingHandler`` だけが使う。ADR-0038）
    assessments: tuple[AssessmentOutcome, ...] = ()
    #: 上限・期限・停止・評価器なしで**実行しなかった**評価の ``task_id``
    assessments_skipped: tuple[str, ...] = ()
    #: 実行器が台帳を通して行った解釈の結果（``InterpretingHandler`` だけが使う。ADR-0039）
    interpretations: tuple[InterpretationOutcome, ...] = ()
    #: 期限・停止・解釈器なしで**実行しなかった**解釈の ``task_id``
    interpretations_skipped: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HandlerOutput:
    """``synthesize`` の出力。実行器はこれを検査してから正準 JSON で保存する。"""

    artifact_type: ResearchArtifactType
    schema_version: str
    #: JSON にできる dict。``request_id`` はこの依頼の ID でなければならない
    artifact: Mapping[str, Any]
    #: 依頼の全項目を評価できたか。偽なら ``partial``（``blocked`` / ``failed`` は実行器が決める）
    complete: bool
    coverage: ResearchCoverage
    warnings: tuple[str, ...] = ()


class ResearchHandler(Protocol):
    @property
    def kind(self) -> ResearchKind: ...

    @property
    def artifact_type(self) -> ResearchArtifactType: ...

    def plan_searches(self, spec: ResearchSpec, *, max_searches: int) -> tuple[SearchStep, ...]:
        """実行したい検索を優先順で返す。上限を超えた分は実行器が落として ``partial`` にする。"""
        ...

    def select_fetches(
        self,
        spec: ResearchSpec,
        rounds: Sequence[SearchRound],
        *,
        remaining: int,
        already_fetched: Collection[str],
    ) -> tuple[FetchTarget, ...]:
        """検索結果から本文を取得する候補を優先順で返す（``remaining`` 件まで）。"""
        ...

    def synthesize(
        self,
        spec: ResearchSpec,
        ctx: SynthesisContext,
        rounds: Sequence[SearchRound],
        fetched: Sequence[FetchedSource],
    ) -> HandlerOutput: ...


@runtime_checkable
class AssessingHandler(ResearchHandler, Protocol):
    """評価器（``ResearchCall.ASSESS``）を使う Handler。"""

    def plan_assessments(
        self,
        spec: ResearchSpec,
        rounds: Sequence[SearchRound],
        fetched: Sequence[FetchedSource],
    ) -> tuple[AssessmentTask, ...]:
        """行いたい評価を優先順で返す（決定的）。上限を超えた分は実行器が落とす。"""
        ...


@runtime_checkable
class InterpretingHandler(ResearchHandler, Protocol):
    """解釈器（``ResearchCall.ASSESS``。Trend の ``TrendInterpreter``）を使う Handler。"""

    def plan_interpretation(
        self,
        spec: ResearchSpec,
        rounds: Sequence[SearchRound],
        fetched: Sequence[FetchedSource],
    ) -> InterpretationTask | None:
        """行いたい解釈（決定的）。解釈する観測が無ければ ``None``（解釈器を呼ばない）。"""
        ...


def plan_within_ceiling(
    steps: Sequence[SearchStep], *, max_searches: int
) -> tuple[tuple[SearchStep, ...], tuple[str, ...]]:
    """(実行する検索, 上限を超えて実行しない検索の ID)。ID の重複・不正は ``ValueError``。"""
    ids = [step.step_id for step in steps]
    for step_id in ids:
        if not STEP_ID_PATTERN.match(step_id):
            raise ValueError(f"invalid search step id: {step_id!r}")
    if len(set(ids)) != len(ids):
        raise ValueError("the handler planned duplicate search step ids")
    limit = max(max_searches, 0)
    return tuple(steps[:limit]), tuple(ids[limit:])


def dedupe_fetch_targets(
    targets: Sequence[FetchTarget], *, already_fetched: Collection[str], remaining: int
) -> tuple[FetchTarget, ...]:
    """正規化 URL で重複を落とし、``remaining`` 件で切る（Handler の選択を信用しない）。"""
    seen = {normalize_url(url) for url in already_fetched}
    chosen: list[FetchTarget] = []
    for target in targets:
        if len(chosen) >= remaining:
            break
        key = normalize_url(target.hit.url)
        if key in seen:
            continue
        seen.add(key)
        chosen.append(target)
    return tuple(chosen)
