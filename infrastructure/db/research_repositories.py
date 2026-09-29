"""Research の永続化（ADR-0037）。本番のリポジトリ（``repositories.py``）とは別のモジュール。

- 本番の表・本番のリポジトリ・課金コードに触れない（INV-37。
  ``tests/architecture/test_research_isolation.py``）
- 状態遷移は ``domain/research/status.py`` / ``domain/research/calls.py`` の表を必ず通し、
  読んだ状態のままの行だけを進める（compare-and-set）
- commit は呼び出し側の責務（本番のリポジトリと同じ）。外部呼び出しの予約は、呼び出しの前に
  commit すること（INV-15 と同じ規律）
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.research import (
    EvidenceResearchRequest,
    ResearchArtifactType,
    ResearchCall,
    ResearchCallStatus,
    ResearchKind,
    ResearchLimits,
    ResearchResult,
    ResearchStatus,
    TrendResearchRequest,
    call_ceiling,
)
from domain.episode.transitions import Rejected
from domain.errors import InvalidTransitionError
from domain.research.calls import CallEvent, transition_call
from domain.research.entities import ResearchArtifactRecord, ResearchCallRecord, ResearchRequest
from domain.research.errors import (
    ResearchAmbiguousCallError,
    ResearchBudgetExceededError,
    ResearchIdempotencyConflictError,
)
from domain.research.identity import request_hash
from domain.research.ids import research_request_id_for
from domain.research.keys import research_artifact_object_key
from domain.research.status import ResearchEvent, transition_research
from infrastructure.db.models import ResearchArtifactRow, ResearchCallRow, ResearchRequestRow

__all__ = [
    "ResearchArtifactRepository",
    "ResearchCallRepository",
    "ResearchRequestRepository",
]

#: 採番の衝突（別の書き手が同じ番号を先に取った）を読み直す回数の上限。上限に達しても
#: 行は作らない（DB の一意制約が最後の砦。INV-36）。
_MAX_SEQ_RETRIES = 8


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _now() -> datetime:
    return datetime.now(UTC)


def _to_request(row: ResearchRequestRow) -> ResearchRequest:
    return ResearchRequest(
        id=str(row.id),
        idempotency_key=row.idempotency_key,
        request_hash=row.request_hash,
        kind=ResearchKind(row.kind),
        status=ResearchStatus(row.status),
        requester=row.requester,
        channel_id=row.channel_id,
        episode_id=str(row.episode_id) if row.episode_id is not None else None,
        payload=row.payload,
        limits=row.limits,
        policy_version=row.policy_version,
        schema_version=row.schema_version,
        provider_config_version=row.provider_config_version,
        as_of=row.as_of,
        created_at=row.created_at,
        updated_at=row.updated_at,
        blocked_reason=row.blocked_reason,
        result_summary=row.result_summary,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _to_call(row: ResearchCallRow) -> ResearchCallRecord:
    return ResearchCallRecord(
        id=str(row.id),
        request_id=str(row.request_id),
        call=ResearchCall(row.provider_call),
        call_seq=row.call_seq,
        idempotency_key=row.idempotency_key,
        input_hash=row.input_hash,
        provider=row.provider,
        status=ResearchCallStatus(row.status),
        reserved_at=row.reserved_at,
        estimated_cost_usd=row.estimated_cost_usd,
        quota_units=row.quota_units,
        dispatched_at=row.dispatched_at,
        settled_at=row.settled_at,
        error_summary=row.error_summary,
    )


def _to_artifact(row: ResearchArtifactRow) -> ResearchArtifactRecord:
    return ResearchArtifactRecord(
        id=str(row.id),
        request_id=str(row.request_id),
        artifact_type=ResearchArtifactType(row.artifact_type),
        schema_version=row.schema_version,
        bucket=row.bucket,
        object_key=row.object_key,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        version=row.version,
        created_at=row.created_at,
        superseded_at=row.superseded_at,
    )


_FINISH_EVENTS: dict[ResearchStatus, ResearchEvent] = {
    ResearchStatus.COMPLETED: ResearchEvent.COMPLETED,
    ResearchStatus.PARTIAL: ResearchEvent.PARTIAL,
    ResearchStatus.BLOCKED: ResearchEvent.BLOCKED,
    ResearchStatus.FAILED: ResearchEvent.FAILED,
}


class ResearchRequestRepository:
    """調査依頼（ADR-0037 §2 / §3）。commit は呼び出し側の責務。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, request_id: uuid.UUID | str) -> ResearchRequestRow | None:
        return await self._session.get(
            ResearchRequestRow, _as_uuid(request_id), populate_existing=True
        )

    async def _by_key(self, idempotency_key: str) -> ResearchRequestRow | None:
        result = await self._session.execute(
            select(ResearchRequestRow)
            .where(ResearchRequestRow.idempotency_key == idempotency_key)
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    @staticmethod
    def _same_request(
        row: ResearchRequestRow, idempotency_key: str, expected_hash: str
    ) -> ResearchRequest:
        if row.request_hash != expected_hash:
            raise ResearchIdempotencyConflictError(
                f"idempotency key {idempotency_key!r} was already used for a different request"
            )
        return _to_request(row)

    async def create_or_get(
        self,
        *,
        idempotency_key: str,
        spec: TrendResearchRequest | EvidenceResearchRequest,
        provider_config_version: str,
    ) -> ResearchRequest:
        """insert-or-get。``id = uuid5(RESEARCH_NAMESPACE, idempotency_key)``。

        ``request_hash`` はここで ``spec`` から計算する（保存する payload と hash が食い違わない）。
        同じキー・同じ hash の再送は既存の行を返す。同じキーで hash が違えば
        ``ResearchIdempotencyConflictError``（保存済みの行は変えない）。別セッションが同じキーを
        先に INSERT した競合は savepoint を戻して読み直す。
        """
        expected_hash = request_hash(spec, provider_config_version=provider_config_version)
        existing = await self._by_key(idempotency_key)
        if existing is not None:
            return self._same_request(existing, idempotency_key, expected_hash)
        now = _now()
        row = ResearchRequestRow(
            id=uuid.UUID(research_request_id_for(idempotency_key)),
            idempotency_key=idempotency_key,
            request_hash=expected_hash,
            kind=spec.kind.value,
            status=ResearchStatus.QUEUED.value,
            requester=spec.requester,
            channel_id=spec.channel_id,
            episode_id=_as_uuid(spec.episode_id) if spec.episode_id is not None else None,
            payload=spec.model_dump(mode="json"),
            limits=spec.limits.model_dump(mode="json"),
            policy_version=spec.policy_version,
            schema_version=spec.schema_version,
            provider_config_version=provider_config_version,
            as_of=spec.as_of,
            created_at=now,
            updated_at=now,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError:
            winner = await self._by_key(idempotency_key)
            if winner is None:
                raise
            return self._same_request(winner, idempotency_key, expected_hash)
        return _to_request(row)

    async def get(self, request_id: uuid.UUID | str) -> ResearchRequest | None:
        row = await self._row(request_id)
        return _to_request(row) if row else None

    async def get_by_idempotency_key(self, idempotency_key: str) -> ResearchRequest | None:
        row = await self._by_key(idempotency_key)
        return _to_request(row) if row else None

    async def _apply(
        self, request_id: uuid.UUID | str, event: ResearchEvent, values: dict[str, Any]
    ) -> ResearchRequest:
        """遷移表を通し、読んだ状態のままの行だけを進める（compare-and-set）。"""
        row = await self._row(request_id)
        if row is None:
            raise InvalidTransitionError(f"research request not found: {request_id}")
        current = ResearchStatus(row.status)
        target = transition_research(current, event)
        if isinstance(target, Rejected):
            raise InvalidTransitionError(target.reason)
        outcome = await self._session.execute(
            update(ResearchRequestRow)
            .where(ResearchRequestRow.id == row.id, ResearchRequestRow.status == current.value)
            .values(status=target.value, updated_at=_now(), **values)
            .execution_options(synchronize_session=False)
        )
        fresh = await self._row(request_id)
        if getattr(outcome, "rowcount", 0) != 1 or fresh is None:
            actual = fresh.status if fresh is not None else "missing"
            raise InvalidTransitionError(
                f"research transition rejected: concurrent change "
                f"(expected {current.value}, found {actual}) + {event.value}"
            )
        return _to_request(fresh)

    async def mark_running(self, request_id: uuid.UUID | str) -> ResearchRequest:
        """``queued → running``。すでに ``running`` なら何もしない（Activity の再実行）。"""
        row = await self._row(request_id)
        if row is not None and ResearchStatus(row.status) is ResearchStatus.RUNNING:
            return _to_request(row)
        return await self._apply(request_id, ResearchEvent.STARTED, {"started_at": _now()})

    async def finish(
        self,
        request_id: uuid.UUID | str,
        status: ResearchStatus,
        *,
        result: ResearchResult | None = None,
        blocked_reason: Mapping[str, Any] | None = None,
    ) -> ResearchRequest:
        """``running → completed | partial | blocked | failed``。commit しない。

        - ``completed`` / ``partial`` は ``result`` 必須。``result`` が指す成果物は、この依頼の
          **現行の** ``research_artifacts`` の行でなければならない（成果物が先、状態が後。
          状態だけ先に進んだ半端な依頼を作らない）
        - ``blocked`` は ``blocked_reason``（``code`` を持つ dict）必須。終端ではない
        """
        event = _FINISH_EVENTS.get(status)
        if event is None:
            raise InvalidTransitionError(f"research request cannot finish as {status.value}")
        if result is not None:
            if result.request_id != str(_as_uuid(request_id)):
                raise ValueError("result describes a different research request")
            if result.execution_status is not status:
                raise ValueError(
                    f"result status {result.execution_status.value} != finish status {status.value}"
                )
        if status in {ResearchStatus.COMPLETED, ResearchStatus.PARTIAL}:
            if result is None:
                raise ValueError(f"a {status.value} research request needs its result")
            await self._require_current_artifacts(request_id, result)
        if status is ResearchStatus.BLOCKED and not (
            blocked_reason and isinstance(blocked_reason.get("code"), str)
        ):
            raise ValueError("a blocked research request needs blocked_reason with a code")
        values: dict[str, Any] = {
            "result_summary": result.model_dump(mode="json") if result is not None else None,
            "blocked_reason": dict(blocked_reason) if blocked_reason is not None else None,
        }
        if status is not ResearchStatus.BLOCKED:
            values["finished_at"] = _now()
        return await self._apply(request_id, event, values)

    async def _require_current_artifacts(
        self, request_id: uuid.UUID | str, result: ResearchResult
    ) -> None:
        for ref in result.artifact_refs:
            found = await self._session.scalar(
                select(ResearchArtifactRow.id).where(
                    ResearchArtifactRow.id == _as_uuid(ref.artifact_id),
                    ResearchArtifactRow.request_id == _as_uuid(request_id),
                    ResearchArtifactRow.artifact_type == ref.artifact_type.value,
                    ResearchArtifactRow.sha256 == ref.sha256,
                    ResearchArtifactRow.superseded_at.is_(None),
                )
            )
            if found is None:
                raise InvalidTransitionError(
                    f"research result references an unrecorded artifact: {ref.artifact_id}"
                )

    async def resume(self, request_id: uuid.UUID | str) -> ResearchRequest:
        """``blocked → queued``（人が原因を直して再開する）。停止理由は消す。"""
        return await self._apply(
            request_id,
            ResearchEvent.RESUMED,
            {"blocked_reason": None, "finished_at": None, "result_summary": None},
        )

    async def list_unstarted(self, older_than: datetime) -> list[ResearchRequest]:
        """``queued`` のまま ``older_than`` より古い依頼（workflow の開始漏れ。回収対象）。"""
        result = await self._session.execute(
            select(ResearchRequestRow)
            .where(
                ResearchRequestRow.status == ResearchStatus.QUEUED.value,
                ResearchRequestRow.created_at < older_than,
            )
            .order_by(ResearchRequestRow.created_at, ResearchRequestRow.id)
            .execution_options(populate_existing=True)
        )
        return [_to_request(row) for row in result.scalars()]

    async def find_reusable(
        self, request_hash: str, not_older_than: datetime
    ) -> ResearchRequest | None:
        """同じ意味（``request_hash``）で ``not_older_than`` 以降に ``completed`` した最新の依頼。

        ``partial`` は合格ではないので再利用しない。現行の成果物を持たない行も返さない。
        鮮度の窓（Trend 24h / Evidence の再確認期限）は呼び出し側が ``not_older_than`` に
        変換して渡す。**成果物の実体の再検証は呼び出し側**（ArtifactStore を読む層）が行う
        （base INV-31 と同じ考え方。ADR-0037 §5）。
        """
        has_current = exists().where(
            ResearchArtifactRow.request_id == ResearchRequestRow.id,
            ResearchArtifactRow.superseded_at.is_(None),
        )
        result = await self._session.execute(
            select(ResearchRequestRow)
            .where(
                ResearchRequestRow.request_hash == request_hash,
                ResearchRequestRow.status == ResearchStatus.COMPLETED.value,
                ResearchRequestRow.finished_at >= not_older_than,
                has_current,
            )
            .order_by(ResearchRequestRow.finished_at.desc(), ResearchRequestRow.id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
        row = result.scalars().first()
        return _to_request(row) if row else None


class ResearchCallRepository:
    """外部呼び出しの台帳（ADR-0037 §4 / INV-36）。commit は呼び出し側の責務。

    使い方: ``reserve`` → commit → ``mark_dispatched`` → commit → 外部呼び出し →
    ``mark_spent`` → commit。呼ばずに終えるなら ``mark_abandoned``（dispatch 前だけ）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _by_key(self, idempotency_key: str) -> ResearchCallRow | None:
        result = await self._session.execute(
            select(ResearchCallRow)
            .where(ResearchCallRow.idempotency_key == idempotency_key)
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def _max_seq(self, request_uuid: uuid.UUID, call: ResearchCall) -> int:
        highest = await self._session.scalar(
            select(func.max(ResearchCallRow.call_seq)).where(
                ResearchCallRow.request_id == request_uuid,
                ResearchCallRow.provider_call == call.value,
            )
        )
        return int(highest or 0)

    async def _spent_so_far(self, request_uuid: uuid.UUID) -> tuple[Decimal, int]:
        """費用・quota を使ったかもしれない行（``abandoned`` 以外）の見積りの合計。"""
        row = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(ResearchCallRow.estimated_cost_usd), 0),
                    func.coalesce(func.sum(ResearchCallRow.quota_units), 0),
                ).where(
                    ResearchCallRow.request_id == request_uuid,
                    ResearchCallRow.status != ResearchCallStatus.ABANDONED.value,
                )
            )
        ).one()
        return Decimal(str(row[0])), int(row[1])

    async def reserve(
        self,
        *,
        request_id: uuid.UUID | str,
        call: ResearchCall,
        idempotency_key: str,
        input_hash: str,
        provider: str,
        estimated_cost_usd: Decimal | None = None,
        quota_units: int | None = None,
    ) -> ResearchCallRecord:
        """外部呼び出しを 1 件予約する。件数の上限は依頼に凍結した ``limits`` から決まる（INV-36）。

        1. 同じ ``idempotency_key`` の行があればそれを返す（**再実行は枠を消費しない**）。
           依頼・種別・``input_hash`` が違えば ``ResearchIdempotencyConflictError``
        2. 依頼が ``running`` でなければ ``InvalidTransitionError``（呼び出しは実行中だけ）
        3. 同じ入力に成否不明の行（dispatch 済み・未決着）があれば ``ResearchAmbiguousCallError``
           （その入力だけを止める）
        4. 金額・quota の上限（設定されていれば）を ``abandoned`` 以外の見積りの合計で超えるなら
           ``ResearchBudgetExceededError``
        5. ``next = max(call_seq) + 1``。``next > call_ceiling`` なら INSERT せず
           ``ResearchBudgetExceededError``。どの状態の行も枠を数え、番号は再利用しない
        6. savepoint 内で INSERT。``IntegrityError``（別の書き手が同じ番号を先に取った）は読み直す。
           DB の ``UNIQUE(request_id, provider_call, call_seq)`` が最後の砦
        """
        request_uuid = _as_uuid(request_id)
        last_error: IntegrityError | None = None
        last_attempted = 0
        for _ in range(_MAX_SEQ_RETRIES):
            existing = await self._by_key(idempotency_key)
            if existing is not None:
                if (
                    existing.request_id != request_uuid
                    or existing.provider_call != call.value
                    or existing.input_hash != input_hash
                ):
                    raise ResearchIdempotencyConflictError(
                        f"idempotency key {idempotency_key!r} is already used by a different call"
                    )
                return _to_call(existing)

            request = await self._session.get(
                ResearchRequestRow, request_uuid, populate_existing=True
            )
            if request is None:
                raise InvalidTransitionError(f"research request not found: {request_id}")
            if request.status != ResearchStatus.RUNNING.value:
                raise InvalidTransitionError(
                    f"research calls can be reserved only while running (found {request.status})"
                )
            limits = ResearchLimits.model_validate(request.limits)

            if await self._has_ambiguous(request_uuid, call, input_hash):
                raise ResearchAmbiguousCallError(
                    f"a {call.value} call with the same input may already have been sent "
                    "and has no outcome; refusing to send it again"
                )
            cost_so_far, units_so_far = await self._spent_so_far(request_uuid)
            self._check_money(limits, cost_so_far, units_so_far, estimated_cost_usd, quota_units)

            seen = await self._max_seq(request_uuid, call)
            next_seq = seen + 1
            ceiling = call_ceiling(limits, call)
            if next_seq > ceiling:
                raise ResearchBudgetExceededError(
                    f"{call.value} call budget exhausted ({seen}/{ceiling}) "
                    f"for research request {request_uuid}"
                )
            if last_error is not None and next_seq <= last_attempted:
                # 衝突したのに番号が進まない = 番号以外の制約違反。読み直しで直らないので送出する
                raise last_error
            row = ResearchCallRow(
                id=uuid.uuid4(),
                request_id=request_uuid,
                provider_call=call.value,
                call_seq=next_seq,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                provider=provider,
                status=ResearchCallStatus.RESERVED.value,
                estimated_cost_usd=estimated_cost_usd,
                quota_units=quota_units,
                reserved_at=_now(),
            )
            try:
                async with self._session.begin_nested():
                    self._session.add(row)
                    await self._session.flush()
            except IntegrityError as exc:
                last_error, last_attempted = exc, next_seq
                continue  # 別の書き手が同じ番号・同じキーを先に取った。読み直す
            return _to_call(row)
        raise ResearchBudgetExceededError(
            f"could not allocate a {call.value} call number for research request {request_uuid} "
            f"after {_MAX_SEQ_RETRIES} attempts"
        )

    @staticmethod
    def _check_money(
        limits: ResearchLimits,
        cost_so_far: Decimal,
        units_so_far: int,
        estimated_cost_usd: Decimal | None,
        quota_units: int | None,
    ) -> None:
        if (
            limits.max_cost_usd is not None
            and estimated_cost_usd is not None
            and cost_so_far + estimated_cost_usd > limits.max_cost_usd
        ):
            raise ResearchBudgetExceededError(
                f"research cost budget exceeded: {cost_so_far} + {estimated_cost_usd} "
                f"> {limits.max_cost_usd}"
            )
        if (
            limits.max_youtube_units is not None
            and quota_units is not None
            and units_so_far + quota_units > limits.max_youtube_units
        ):
            raise ResearchBudgetExceededError(
                f"research quota budget exceeded: {units_so_far} + {quota_units} "
                f"> {limits.max_youtube_units}"
            )

    async def _has_ambiguous(
        self, request_uuid: uuid.UUID, call: ResearchCall, input_hash: str
    ) -> bool:
        found = await self._session.scalar(
            select(ResearchCallRow.id)
            .where(
                ResearchCallRow.request_id == request_uuid,
                ResearchCallRow.provider_call == call.value,
                ResearchCallRow.input_hash == input_hash,
                ResearchCallRow.status == ResearchCallStatus.RESERVED.value,
                ResearchCallRow.dispatched_at.is_not(None),
            )
            .limit(1)
        )
        return found is not None

    async def _row(self, call_id: uuid.UUID | str) -> ResearchCallRow:
        row = await self._session.get(ResearchCallRow, _as_uuid(call_id), populate_existing=True)
        if row is None:
            raise InvalidTransitionError(f"research call not found: {call_id}")
        return row

    async def mark_dispatched(self, call_id: uuid.UUID | str) -> ResearchCallRecord:
        """外部呼び出しの**直前**に呼び、commit してから呼ぶ。

        ``reserved`` かつ未 dispatch の行だけを1文の条件付き UPDATE で進める。更新0行は
        「既に送ったかもしれない行」なので ``ResearchAmbiguousCallError``（人手照合）。
        """
        row = await self._row(call_id)
        outcome = await self._session.execute(
            update(ResearchCallRow)
            .where(
                ResearchCallRow.id == row.id,
                ResearchCallRow.status == ResearchCallStatus.RESERVED.value,
                ResearchCallRow.dispatched_at.is_(None),
            )
            .values(dispatched_at=_now())
            .execution_options(synchronize_session=False)
        )
        if getattr(outcome, "rowcount", 0) != 1:
            raise ResearchAmbiguousCallError(
                f"research call {call_id} was already dispatched or settled; not sending again"
            )
        return _to_call(await self._row(call_id))

    async def _settle(
        self, call_id: uuid.UUID | str, event: CallEvent, values: dict[str, Any]
    ) -> ResearchCallRecord:
        row = await self._row(call_id)
        current = ResearchCallStatus(row.status)
        target = transition_call(current, event, dispatched=row.dispatched_at is not None)
        if isinstance(target, Rejected):
            raise InvalidTransitionError(target.reason)
        conditions = [ResearchCallRow.id == row.id, ResearchCallRow.status == current.value]
        if target is ResearchCallStatus.ABANDONED:
            conditions.append(ResearchCallRow.dispatched_at.is_(None))
        outcome = await self._session.execute(
            update(ResearchCallRow)
            .where(*conditions)
            .values(status=target.value, settled_at=_now(), **values)
            .execution_options(synchronize_session=False)
        )
        if getattr(outcome, "rowcount", 0) != 1:
            raise InvalidTransitionError(
                f"research call transition rejected: concurrent change + {event.value}"
            )
        return _to_call(await self._row(call_id))

    async def mark_spent(
        self,
        call_id: uuid.UUID | str,
        *,
        error_summary: str | None = None,
    ) -> ResearchCallRecord:
        """呼んだ（成否にかかわらず）。失敗した呼び出しも課金されたものとして数える。"""
        return await self._settle(
            call_id, CallEvent.SPENT, {"error_summary": (error_summary or "")[:1000] or None}
        )

    async def mark_abandoned(self, call_id: uuid.UUID | str, *, reason: str) -> ResearchCallRecord:
        """送っていないことが確かな予約を手放す（dispatch 前だけ）。枠は戻らない。"""
        return await self._settle(
            call_id, CallEvent.ABANDONED, {"error_summary": reason[:1000] or None}
        )

    async def list_for_request(self, request_id: uuid.UUID | str) -> list[ResearchCallRecord]:
        result = await self._session.execute(
            select(ResearchCallRow)
            .where(ResearchCallRow.request_id == _as_uuid(request_id))
            .order_by(ResearchCallRow.provider_call, ResearchCallRow.call_seq)
            .execution_options(populate_existing=True)
        )
        return [_to_call(row) for row in result.scalars()]

    async def find_ambiguous(self, request_id: uuid.UUID | str) -> list[ResearchCallRecord]:
        """dispatch 済みで決着していない行（成否不明。自動で再送も解放もしない）。"""
        result = await self._session.execute(
            select(ResearchCallRow)
            .where(
                ResearchCallRow.request_id == _as_uuid(request_id),
                ResearchCallRow.status == ResearchCallStatus.RESERVED.value,
                ResearchCallRow.dispatched_at.is_not(None),
            )
            .order_by(ResearchCallRow.provider_call, ResearchCallRow.call_seq)
            .execution_options(populate_existing=True)
        )
        return [_to_call(row) for row in result.scalars()]


class ResearchArtifactRepository:
    """research 所有の成果物の世代（ADR-0037 §5）。commit は呼び出し側の責務。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        request_id: uuid.UUID | str,
        artifact_type: ResearchArtifactType,
        schema_version: str,
        bucket: str,
        object_key: str,
        sha256: str,
        size_bytes: int,
    ) -> ResearchArtifactRecord:
        """同じ内容の再記録は既存行を返す（INV-17）。新しい内容は新しい世代にして現行を降ろす。

        ``object_key`` はこの依頼・型・sha256 の research キー
        （``research_artifact_object_key``）でなければならない（Episode の ``artifacts/`` 配下や
        別依頼のキーを記録しない）。書き込み・読み戻し・sha256 照合は呼び出し側が先に済ませる。
        """
        request_uuid = _as_uuid(request_id)
        expected_key = research_artifact_object_key(str(request_uuid), artifact_type, sha256)
        if object_key != expected_key:
            raise ValueError(f"research artifact key must be {expected_key!r}, got {object_key!r}")
        if await self._session.get(ResearchRequestRow, request_uuid) is None:
            raise InvalidTransitionError(f"research request not found: {request_id}")

        existing = (
            await self._session.scalars(
                select(ResearchArtifactRow).where(
                    ResearchArtifactRow.request_id == request_uuid,
                    ResearchArtifactRow.artifact_type == artifact_type.value,
                    ResearchArtifactRow.sha256 == sha256,
                )
            )
        ).first()
        now = _now()
        if existing is not None:
            if existing.superseded_at is not None:
                # A→B→A: 過去世代を現行へ戻す（現行を先に降ろす）
                await self._supersede_current(request_uuid, artifact_type, now)
                existing.superseded_at = None
                await self._session.flush()
            return _to_artifact(existing)

        max_version = await self._session.scalar(
            select(func.max(ResearchArtifactRow.version)).where(
                ResearchArtifactRow.request_id == request_uuid,
                ResearchArtifactRow.artifact_type == artifact_type.value,
            )
        )
        await self._supersede_current(request_uuid, artifact_type, now)
        row = ResearchArtifactRow(
            id=uuid.uuid4(),
            request_id=request_uuid,
            artifact_type=artifact_type.value,
            schema_version=schema_version,
            bucket=bucket,
            object_key=object_key,
            sha256=sha256,
            size_bytes=size_bytes,
            version=(max_version or 0) + 1,
            created_at=now,
            superseded_at=None,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_artifact(row)

    async def _supersede_current(
        self, request_uuid: uuid.UUID, artifact_type: ResearchArtifactType, now: datetime
    ) -> None:
        """現行世代を降ろす唯一の場所。部分一意索引より先に flush する。"""
        current = await self._session.scalars(
            select(ResearchArtifactRow).where(
                ResearchArtifactRow.request_id == request_uuid,
                ResearchArtifactRow.artifact_type == artifact_type.value,
                ResearchArtifactRow.superseded_at.is_(None),
            )
        )
        for row in current.all():
            row.superseded_at = now
        await self._session.flush()

    async def find_current(
        self, request_id: uuid.UUID | str, artifact_type: ResearchArtifactType
    ) -> ResearchArtifactRecord | None:
        row = (
            await self._session.scalars(
                select(ResearchArtifactRow)
                .where(
                    ResearchArtifactRow.request_id == _as_uuid(request_id),
                    ResearchArtifactRow.artifact_type == artifact_type.value,
                    ResearchArtifactRow.superseded_at.is_(None),
                )
                .execution_options(populate_existing=True)
            )
        ).first()
        return _to_artifact(row) if row else None

    async def list_current(self, request_id: uuid.UUID | str) -> list[ResearchArtifactRecord]:
        rows = await self._session.scalars(
            select(ResearchArtifactRow)
            .where(
                ResearchArtifactRow.request_id == _as_uuid(request_id),
                ResearchArtifactRow.superseded_at.is_(None),
            )
            .order_by(ResearchArtifactRow.artifact_type)
            .execution_options(populate_existing=True)
        )
        return [_to_artifact(row) for row in rows.all()]
