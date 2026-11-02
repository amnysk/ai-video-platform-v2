"""BGM カタログと Episode ごとの選曲の PostgreSQL 実装（ADR-0087 §4、INV-153）。

``domain.music.ports.BgmCatalogRepository`` を実装する。各メソッドは**自分の**トランザクションを持つ
（呼び出し側は session を持たない）。``async_sessionmaker`` を注入する。

- ``save_selection_once`` は ``episode_id`` の UNIQUE が最終的な門で、同時に呼ばれても
  1 行だけ残り、負けた側は**先に入った行を返す**（上書きしない）
- ``finalize_selection`` は ``WHERE finalized = false`` の条件つき UPDATE（原子的）。確定済みなら既存を返す
- 日時は UTC に揃えて書き、読むときは aware にする（SQLite は tz を落とすため）
- ライセンス事実を黙って緩めない: ``register_quarantined`` は隔離でない曲を拒否し、
  ``promote`` は隔離中の曲だけを（同じ sha256 のまま）承認済みにする
"""  # noqa: E501

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.music import BgmTrack
from contracts.music_selection import BgmSelectionDecision
from contracts.music_sources import ACQUISITION_MIN_INTERVAL_SECONDS, AcquisitionRunSummary
from domain.music.ports import CatalogEntry
from infrastructure.db.models import (
    BgmAcquisitionDemandRow,
    BgmAcquisitionRunRow,
    BgmSelectionRow,
    BgmTrackRow,
)

_TRACK_FIELDS = tuple(BgmTrack.model_fields)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _track_values(track: BgmTrack) -> dict[str, Any]:
    """``BgmTrack`` → 列の値。タプルは JSON の配列、日時は UTC。"""
    values: dict[str, Any] = {}
    for name in _TRACK_FIELDS:
        value = getattr(track, name)
        if isinstance(value, tuple):
            value = list(value)
        elif isinstance(value, datetime):
            value = _utc(value)
        values[name] = value
    return values


def _track_from_row(row: BgmTrackRow) -> BgmTrack:
    data: dict[str, Any] = {}
    for name in _TRACK_FIELDS:
        value = getattr(row, name)
        if isinstance(value, datetime):
            value = _aware(value)
        data[name] = value
    return BgmTrack.model_validate(data)


def _entry_from_row(row: BgmTrackRow) -> CatalogEntry:
    acquired_at = _aware(row.acquired_at)
    assert acquired_at is not None
    return CatalogEntry(
        track=_track_from_row(row),
        acquired_from=row.acquired_from,
        acquired_at=acquired_at,
        evidence_object_key=row.evidence_object_key,
        analysis=dict(row.analysis),
        inferred_tags=dict(row.inferred_tags),
        last_verified_at=_aware(row.last_verified_at),
    )


def _selection_values(decision: BgmSelectionDecision) -> dict[str, Any]:
    return {
        "track_id": decision.track_id,
        "cue_sheet_sha256": decision.cue_sheet_sha256,
        "requirements_sha256": decision.requirements_sha256,
        "rule_version": decision.rule_version,
        "decision": decision.model_dump(mode="json"),
    }


class SqlBgmCatalogRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------ 読み（選曲）

    async def list_entries(self) -> tuple[CatalogEntry, ...]:
        async with self._session_factory() as session:
            rows = await session.scalars(select(BgmTrackRow).order_by(BgmTrackRow.track_id))
            return tuple(_entry_from_row(r) for r in rows)

    async def find_by_sha256(self, file_sha256: str) -> CatalogEntry | None:
        async with self._session_factory() as session:
            return await self._by_sha(session, file_sha256)

    @staticmethod
    async def _by_sha(session: AsyncSession, file_sha256: str) -> CatalogEntry | None:
        row = await session.scalar(
            select(BgmTrackRow).where(BgmTrackRow.file_sha256 == file_sha256)
        )
        return None if row is None else _entry_from_row(row)

    async def recent_usage(self, *, window: int) -> dict[str, int]:
        """直近 ``window`` 件の**確定済み**の選曲（曲があるもの）で使われた回数。"""
        if window <= 0:
            return {}
        async with self._session_factory() as session:
            ids = await session.scalars(
                select(BgmSelectionRow.track_id)
                .where(BgmSelectionRow.finalized.is_(True), BgmSelectionRow.track_id.is_not(None))
                .order_by(BgmSelectionRow.finalized_at.desc(), BgmSelectionRow.id)
                .limit(window)
            )
            return dict(Counter(i for i in ids if i is not None))

    # ------------------------------------------------------------------ 書き（収集・取込）

    async def register_quarantined(
        self,
        track: BgmTrack,
        *,
        acquired_from: str,
        acquired_at: datetime,
        evidence_object_key: str | None,
        analysis: Mapping[str, Any],
        inferred_tags: Mapping[str, Any],
    ) -> tuple[CatalogEntry, bool]:
        if track.status != "quarantined" or track.license_status != "unverified":
            raise ValueError("a new track must be registered quarantined and unverified (INV-133)")
        async with self._session_factory() as session:
            existing = await self._by_sha(session, track.file_sha256)
            if existing is not None:
                return existing, False
            session.add(
                BgmTrackRow(
                    **_track_values(track),
                    acquired_from=acquired_from,
                    acquired_at=_utc(acquired_at),
                    evidence_object_key=evidence_object_key,
                    analysis=dict(analysis),
                    inferred_tags=dict(inferred_tags),
                    last_verified_at=None,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raced = await self._by_sha(session, track.file_sha256)
                if raced is None:  # 同じ track_id の別の曲。黙って上書きしない
                    raise
                return raced, False
            created = await self._by_sha(session, track.file_sha256)
            assert created is not None
            return created, True

    async def promote(self, track: BgmTrack, *, verified_at: datetime) -> CatalogEntry:
        if track.status != "active" or track.license_status != "approved":
            raise ValueError("promote needs an active, approved track")
        async with self._session_factory() as session:
            row = await session.get(BgmTrackRow, track.track_id)
            if row is None:
                raise LookupError(f"unknown track {track.track_id!r}")
            if row.file_sha256 != track.file_sha256:
                raise ValueError("promote cannot change the content of a track")
            values = _track_values(track)
            del values["track_id"], values["file_sha256"]
            result = await session.execute(
                update(BgmTrackRow)
                .where(BgmTrackRow.track_id == track.track_id, BgmTrackRow.status == "quarantined")
                .values(**values, last_verified_at=_utc(verified_at))
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                await session.rollback()
                raise ValueError(f"track {track.track_id!r} is not quarantined")
            await session.commit()
        entry = await self.find_by_sha256(track.file_sha256)
        assert entry is not None
        return entry

    async def record_acquisition_run(self, summary: AcquisitionRunSummary) -> None:
        async with self._session_factory() as session:
            session.add(
                BgmAcquisitionRunRow(
                    id=uuid.uuid4(),
                    demand_sha256=summary.demand_sha256,
                    started_at=_utc(summary.started_at),
                    finished_at=_utc(summary.finished_at),
                    searches=summary.searches,
                    candidates=[c.model_dump(mode="json") for c in summary.candidates],
                )
            )
            await session.commit()

    async def last_acquisition_at(self, demand_sha256: str) -> datetime | None:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(func.max(BgmAcquisitionRunRow.finished_at)).where(
                    BgmAcquisitionRunRow.demand_sha256 == demand_sha256
                )
            )
            return _aware(value)

    async def request_acquisition(
        self, demand_sha256: str, requirements_json: Mapping[str, Any], *, requested_at: datetime
    ) -> None:
        """同じ hash は 1 行（冪等）。実行済みで 1 日以上経っていれば、需要を再び積む。"""
        at = _utc(requested_at)
        assert at is not None
        async with self._session_factory() as session:
            row = await session.get(BgmAcquisitionDemandRow, demand_sha256)
            if row is None:
                session.add(
                    BgmAcquisitionDemandRow(
                        demand_sha256=demand_sha256,
                        requirements=dict(requirements_json),
                        requested_at=at,
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()  # 同時に積まれた。1 行あれば足りる
                return
            last = _aware(
                await session.scalar(
                    select(func.max(BgmAcquisitionRunRow.finished_at)).where(
                        BgmAcquisitionRunRow.demand_sha256 == demand_sha256
                    )
                )
            )
            served = last is not None and last >= _aware(row.requested_at)  # type: ignore[operator]
            if served and at - last >= timedelta(seconds=ACQUISITION_MIN_INTERVAL_SECONDS):  # type: ignore[operator]
                row.requested_at = at
                await session.commit()

    async def pending_demands(self, *, limit: int) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """まだ実行されていない需要（要求以降に終わった実行が無い）。古い順。"""
        async with self._session_factory() as session:
            served = exists().where(
                BgmAcquisitionRunRow.demand_sha256 == BgmAcquisitionDemandRow.demand_sha256,
                BgmAcquisitionRunRow.finished_at >= BgmAcquisitionDemandRow.requested_at,
            )
            rows = await session.scalars(
                select(BgmAcquisitionDemandRow)
                .where(~served)
                .order_by(
                    BgmAcquisitionDemandRow.requested_at, BgmAcquisitionDemandRow.demand_sha256
                )
                .limit(limit)
            )
            return tuple((r.demand_sha256, dict(r.requirements)) for r in rows)

    # ------------------------------------------------------------------ 選曲の決定

    async def get_selection(self, episode_id: str) -> BgmSelectionDecision | None:
        async with self._session_factory() as session:
            return await self._selection(session, episode_id)

    @staticmethod
    async def _selection(session: AsyncSession, episode_id: str) -> BgmSelectionDecision | None:
        row = await session.scalar(
            select(BgmSelectionRow).where(BgmSelectionRow.episode_id == uuid.UUID(episode_id))
        )
        return None if row is None else BgmSelectionDecision.model_validate(row.decision)

    async def is_selection_finalized(self, episode_id: str) -> bool:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(BgmSelectionRow.finalized).where(
                    BgmSelectionRow.episode_id == uuid.UUID(episode_id)
                )
            )
            return bool(value)

    async def save_selection_once(
        self, decision: BgmSelectionDecision, *, decided_at: datetime
    ) -> BgmSelectionDecision:
        """無ければ挿入し、あれば既存を返す。同時実行でも 1 行だけ（後勝ちしない。INV-153）。"""
        async with self._session_factory() as session:
            session.add(
                BgmSelectionRow(
                    id=uuid.uuid4(),
                    episode_id=uuid.UUID(decision.episode_id),
                    finalized=False,
                    decided_at=_utc(decided_at),
                    finalized_at=None,
                    **_selection_values(decision),
                )
            )
            try:
                await session.commit()
                return decision
            except IntegrityError as exc:
                await session.rollback()
                conflict = exc
            existing = await self._selection(session, decision.episode_id)
            if existing is None:
                raise conflict
            return existing

    async def finalize_selection(
        self, decision: BgmSelectionDecision, *, finalized_at: datetime
    ) -> BgmSelectionDecision:
        """梯子の結果で確定する。確定済みなら既存を返す（原子的な条件つき UPDATE）。"""
        at = _utc(finalized_at)
        episode = uuid.UUID(decision.episode_id)
        async with self._session_factory() as session:
            result = await session.execute(
                update(BgmSelectionRow)
                .where(BgmSelectionRow.episode_id == episode, BgmSelectionRow.finalized.is_(False))
                .values(finalized=True, finalized_at=at, **_selection_values(decision))
            )
            if result.rowcount == 1:  # type: ignore[attr-defined]
                await session.commit()
                return decision
            await session.rollback()
            existing = await self._selection(session, decision.episode_id)
            if existing is not None:
                return existing
            session.add(
                BgmSelectionRow(
                    id=uuid.uuid4(),
                    episode_id=episode,
                    finalized=True,
                    decided_at=at,
                    finalized_at=at,
                    **_selection_values(decision),
                )
            )
            try:
                await session.commit()
                return decision
            except IntegrityError as exc:
                await session.rollback()
                conflict = exc
            raced = await self._selection(session, decision.episode_id)
            if raced is None:
                raise conflict
            return raced


__all__ = ["SqlBgmCatalogRepository"]
