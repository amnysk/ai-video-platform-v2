"""Episode の統一再開の判定（ADR-0032）。

純粋関数のみ。DB・Temporal・HTTP・ファイルI/Oに一切触れない（INV-6）。呼び出し側（API層）が
読み取り済みの値（現在の Episode 状態、記録された owner workflow id、未照合の予約一覧）を渡し、
ここは「どの工程から再開できるか／できないなら何故か」を計算して返すだけ。

workflow の start・予約 INSERT・provider 呼び出しはここでは一切行わない
（``GET /episodes/{id}/resume/plan`` の read-only 性を保証する構造的な理由。
ADR-0032 §Decision(1)）。

工程順序の知識は増やさない: 既存の ``STAGE_PARKING_STATUS``（``contracts.pipeline``）と
``PRODUCTION_ADMISSIBLE_STATUSES`` / ``RENDER_ADMISSIBLE_STATUSES`` / ``UPLOAD_ADMISSIBLE_STATUSES``
（``contracts.states``、各工程の既存 POST エンドポイントが今も使っている admit 表）だけから導く
（AGENTS.md §8）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from contracts.pipeline import (
    STAGE_PARKING_STATUS,
    STAGE_PROVIDERS,
    PipelineStage,
    production_workflow_id,
    render_workflow_id,
    upload_workflow_id,
)
from contracts.states import EpisodeStatus, ProviderCall, ReservationStatus

_STAGE_ORDER: tuple[PipelineStage, ...] = tuple(PipelineStage)


def _next_stage_after_parking_map() -> dict[EpisodeStatus, PipelineStage | None]:
    """駐機点の状態から一意に決まる「次の工程」（``STAGE_PARKING_STATUS`` の逆写像 + 1つ先）。

    ``UPLOADED`` は次が無い（pipeline は既に完了している）。
    """
    mapping: dict[EpisodeStatus, PipelineStage | None] = {}
    for stage, status in STAGE_PARKING_STATUS.items():
        idx = _STAGE_ORDER.index(stage)
        mapping[status] = _STAGE_ORDER[idx + 1] if idx + 1 < len(_STAGE_ORDER) else None
    return mapping


_NEXT_STAGE_AFTER_PARKING: dict[EpisodeStatus, PipelineStage | None] = (
    _next_stage_after_parking_map()
)

#: ``needs_work`` / ``blocked`` の所有権判定に使う工程（production が最初の再開可能工程。
#: script / storyboard は独自の resume 用 POST・所有権記録を持たないため対象外 — ADR-0017 §8 の
#: 既存 admit 表と同じ範囲）。id 関数は ``contracts.pipeline`` の単一宣言元をそのまま使う。
_OWNER_STAGE_ID_FN: dict[PipelineStage, Callable[[str], str]] = {
    PipelineStage.PRODUCTION: production_workflow_id,
    PipelineStage.RENDER: render_workflow_id,
    PipelineStage.UPLOAD: upload_workflow_id,
}


@dataclass(frozen=True, slots=True)
class ReservationSummary:
    """``infrastructure.db.repositories.ProviderReservation`` のうち判定に要る列だけ。

    呼び出し側（API層）がリポジトリから読んで渡す。ここでは DB に触れない。
    """

    id: str
    provider: ProviderCall
    status: ReservationStatus
    raw_output_key: str | None


def _is_unreconciled(reservation: ReservationSummary) -> bool:
    """``ProviderReservationRepository.find_unreconciled`` と同じ述語。

    evidence（生出力）の無い ``reserved`` だけを未照合とみなす。
    """
    return reservation.status is ReservationStatus.RESERVED and reservation.raw_output_key is None


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """``GET /episodes/{id}/resume/plan`` と ``POST /episodes/{id}/resume`` が共有する計画。"""

    episode_id: str
    resumable: bool
    #: 次に入る工程（``PipelineStage.value``）。resumable が False なら None。
    target_stage: str | None
    #: target_stage から UPLOAD までの工程（値の列）。resumable が False なら空。
    stages_to_run: tuple[str, ...]
    #: 再開を止めている理由（人が読める文の列）。空なら resumable。
    unresolved_blockers: tuple[str, ...]
    #: 成否不明で残っている予約（evidence 無しの reserved）。
    unreconciled_reservations: tuple[ReservationSummary, ...] = field(default_factory=tuple)
    #: ``stages_to_run`` のうち非空の ``STAGE_PROVIDERS`` を持つ工程（値の列）。
    #:
    #: **スコープを限定した開示であって保証ではない**: 実際に課金するかどうかは各工程の
    #: Activity が現行 Artifact の ``input_hash`` を見て決める（INV-17。既に自動で効いている）。
    #: ここでは「この工程には有料 provider が関わるので、新しいラウンドが要れば課金されうる」
    #: ことを一覧するだけで、上流 Artifact の現行 ``input_hash`` と過去の呼び出しの
    #: ``input_hash`` を比較して「実際に新しい課金が起きるか」を判定してはいない
    #: （そのためには工程ごとの Artifact・予約の詳細な読み取りが要り、本 ADR のスコープ外。
    #: ADR-0032 §Decision / §Consequences に明記）。
    possible_new_charges: tuple[str, ...] = field(default_factory=tuple)
    #: resumable が False のときの代表理由（先頭の blocker と同じ）。人向けの一行要約。
    reason: str | None = None


def determine_target_stage(
    *, status: EpisodeStatus, episode_id: str, owner_workflow_id: str | None
) -> tuple[PipelineStage | None, str | None]:
    """現在の Episode 状態から、再開すべき工程を1つ決める（決められなければ理由付きで None）。

    駐機点の状態（``script_ready`` 等）は一意に次工程が決まる。``needs_work`` / ``blocked`` は
    複数工程がなりうるので、記録された owner workflow id（``EpisodeRepository.get_workflow_id``）を
    ``production_workflow_id`` 等と突き合わせて決める（render / upload の既存 POST エンドポイントが
    今も使っている判定と同じ、ADR-0017 §8 / ADR-0019 / ADR-0020）。決められない場合は推測しない。
    """
    if status in _NEXT_STAGE_AFTER_PARKING:
        next_stage = _NEXT_STAGE_AFTER_PARKING[status]
        if next_stage is None:
            return None, f"episode is {status.value}; the pipeline has already finished"
        return next_stage, None

    if status not in (EpisodeStatus.NEEDS_WORK, EpisodeStatus.BLOCKED):
        return None, f"episode is {status.value}; that is not a resumable status"

    owner = (owner_workflow_id or "").rsplit(":", 1)[0]
    for stage, id_fn in _OWNER_STAGE_ID_FN.items():
        if owner and owner == id_fn(episode_id):
            return stage, None
    return None, (
        f"episode is {status.value} but its owning workflow ({owner or 'unknown'}) "
        "does not match a known resumable stage for this episode; resolve manually"
    )


def build_resume_plan(
    *,
    episode_id: str,
    status: EpisodeStatus,
    owner_workflow_id: str | None,
    unreconciled_reservations: Sequence[ReservationSummary] = (),
) -> ResumePlan:
    """再開計画を組み立てる（ADR-0032 §Decision(1)）。

    ``unreconciled_reservations`` は呼び出し側が対象工程の provider（``STAGE_PROVIDERS``）について
    既に読んでおいた予約の一覧。ここでは evidence の無い ``reserved`` だけを blocker として扱う
    （``ProviderReservationRepository.find_unreconciled`` と同じ述語、INV-15）。
    """
    target_stage, refusal = determine_target_stage(
        status=status, episode_id=episode_id, owner_workflow_id=owner_workflow_id
    )
    blockers: list[str] = []
    if refusal is not None:
        blockers.append(refusal)

    pending = tuple(r for r in unreconciled_reservations if _is_unreconciled(r))
    if pending:
        blockers.append(
            f"{len(pending)} provider reservation(s) are unreconciled (dispatched but no "
            "evidence yet); a human must resolve them before resuming — they are never "
            "auto-resent (INV-15)"
        )

    resumable = target_stage is not None and not blockers
    stages_to_run = (
        tuple(s.value for s in _STAGE_ORDER[_STAGE_ORDER.index(target_stage) :])
        if target_stage is not None
        else ()
    )
    possible_new_charges = tuple(s for s in stages_to_run if STAGE_PROVIDERS[PipelineStage(s)])
    return ResumePlan(
        episode_id=episode_id,
        resumable=resumable,
        target_stage=target_stage.value if target_stage is not None else None,
        stages_to_run=stages_to_run,
        unresolved_blockers=tuple(blockers),
        unreconciled_reservations=pending,
        possible_new_charges=possible_new_charges,
        reason=None if resumable else blockers[0],
    )


__all__ = [
    "STAGE_PROVIDERS",
    "ReservationSummary",
    "ResumePlan",
    "build_resume_plan",
    "determine_target_stage",
]
