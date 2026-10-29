"""品質検査の記録・使用宣言・レポート・手動承認の永続化（ADR-0081 / INV-126〜129）。

**追記専用**。読み出しは必ず ``(episode_id, final_video_sha256)`` を指定する
（「episode の最新」を返す関数を持たない = 別 hash の記録を取り違える口が無い）。
取得した行の鮮度・結び付きの最終判断は ``domain.quality.records.select_fresh_check_sets``
が行い、ここは SQL の絞り込みに頼らない。
方言中立（PostgreSQL / SQLite）。SQLite が返す naive な時刻は UTC として扱う（保存は UTC aware）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.approval import ApproverKind
from contracts.quality import (
    QualityCheck,
    QualityGate,
    QualityReason,
    QualityReport,
    QualityVerdict,
)
from contracts.quality_records import CheckKey, UsageFeature
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.quality.records import (
    CheckSetDraft,
    CheckSetInvalid,
    CheckSetRecord,
    CheckSubject,
    UsageDeclaration,
    new_check_set,
)
from infrastructure.db.models import (
    QualityCheckRecordRow,
    QualityManualApprovalRow,
    QualityReportRow,
    QualityUsageDeclarationRow,
)

logger = logging.getLogger(__name__)

#: 世代の競合（同時に同じ key を書く）で再試行する最大回数
_MAX_GENERATION_RETRIES = 3


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


def _check_from_dict(raw: dict[str, Any]) -> QualityCheck:
    return QualityCheck(
        gate=QualityGate(raw["gate"]),
        verdict=QualityVerdict(raw["verdict"]),
        reason_code=QualityReason(raw["reason_code"]),
        detail=raw.get("detail", ""),
        deterministic=bool(raw["deterministic"]),
        scene_id=raw.get("scene_id"),
        evidence_ref=raw.get("evidence_ref"),
    )


def _record_from_row(row: QualityCheckRecordRow) -> CheckSetRecord | None:
    """壊れた行は ``None``（= 無いものとして扱う。fail-closed）。"""
    try:
        return CheckSetRecord(
            id=str(row.id),
            subject=CheckSubject(str(row.episode_id), row.final_video_sha256, row.input_hash),
            rule_version=row.rule_version,
            key=CheckKey(row.check_key),
            generation=row.generation,
            checks=tuple(_check_from_dict(c) for c in row.checks),
            producer=row.producer,
            producer_version=row.producer_version,
            checked_at=_utc(row.checked_at),
            expires_at=_utc(row.expires_at),
            evidence_ref=row.evidence_ref,
        )
    except (ValueError, KeyError, TypeError, CheckSetInvalid):
        logger.warning("quality check record %s is unreadable; treated as missing", row.id)
        return None


def _usage_from_row(row: QualityUsageDeclarationRow) -> UsageDeclaration | None:
    try:
        return UsageDeclaration(
            subject=CheckSubject(str(row.episode_id), row.final_video_sha256, row.input_hash),
            feature=UsageFeature(row.feature),
            used=row.used,
            declared_by=row.declared_by,
            declared_at=_utc(row.declared_at),
            evidence_ref=row.evidence_ref,
        )
    except (ValueError, CheckSetInvalid):
        logger.warning("quality usage declaration %s is unreadable; treated as missing", row.id)
        return None


class QualityRecordRepository:
    """1 つの ``AsyncSession`` 上の読み書き。commit は呼び出し側。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_check_sets(self, subject: CheckSubject) -> list[CheckSetRecord]:
        """この対象 hash の全世代・全規則版の行。鮮度の判断は呼び出し側（純関数）が行う。"""
        stmt = select(QualityCheckRecordRow).where(
            QualityCheckRecordRow.episode_id == _as_uuid(subject.episode_id),
            QualityCheckRecordRow.final_video_sha256 == subject.final_video_sha256,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [r for r in (_record_from_row(row) for row in rows) if r is not None]

    async def list_usage(self, subject: CheckSubject) -> list[UsageDeclaration]:
        stmt = select(QualityUsageDeclarationRow).where(
            QualityUsageDeclarationRow.episode_id == _as_uuid(subject.episode_id),
            QualityUsageDeclarationRow.final_video_sha256 == subject.final_video_sha256,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [d for d in (_usage_from_row(row) for row in rows) if d is not None]

    async def write_check_set(
        self,
        subject: CheckSubject,
        draft: CheckSetDraft,
        *,
        checked_at: datetime,
        rule_version: str,
    ) -> CheckSetRecord:
        """新しい世代の行を足す（既存の行は書き換えない）。一意制約の競合は世代を上げて再試行。"""
        last: Exception | None = None
        for _ in range(_MAX_GENERATION_RETRIES):
            generation = await self._next_generation(subject, draft.key, rule_version)
            record = new_check_set(
                record_id=str(uuid.uuid4()),
                subject=subject,
                draft=draft,
                rule_version=rule_version,
                generation=generation,
                checked_at=checked_at,
            )
            try:
                async with self._session.begin_nested():
                    self._session.add(
                        QualityCheckRecordRow(
                            id=uuid.UUID(record.id),
                            episode_id=_as_uuid(subject.episode_id),
                            final_video_sha256=subject.final_video_sha256,
                            input_hash=subject.input_hash,
                            rule_version=rule_version,
                            check_key=draft.key.value,
                            generation=generation,
                            verdict=record.verdict.value,
                            checks=[c.to_dict() for c in record.checks],
                            producer=draft.producer,
                            producer_version=draft.producer_version,
                            evidence_ref=draft.evidence_ref,
                            checked_at=record.checked_at,
                            expires_at=record.expires_at,
                        )
                    )
                return record
            except IntegrityError as exc:
                last = exc
        raise RuntimeError("could not allocate a generation for the check set") from last

    async def _next_generation(self, subject: CheckSubject, key: CheckKey, rule: str) -> int:
        stmt = select(func.max(QualityCheckRecordRow.generation)).where(
            QualityCheckRecordRow.episode_id == _as_uuid(subject.episode_id),
            QualityCheckRecordRow.final_video_sha256 == subject.final_video_sha256,
            QualityCheckRecordRow.rule_version == rule,
            QualityCheckRecordRow.check_key == key.value,
        )
        return ((await self._session.execute(stmt)).scalar_one_or_none() or 0) + 1

    async def declare_usage(
        self,
        subject: CheckSubject,
        feature: UsageFeature,
        *,
        used: bool,
        declared_by: str,
        declared_at: datetime,
        evidence_ref: str | None = None,
    ) -> UsageDeclaration:
        """宣言を足す。同じ (対象, 機能) に既にあれば同じ主張のときだけそれを返す（矛盾は例外）。"""
        declaration = UsageDeclaration(
            subject, feature, used, declared_by, declared_at, evidence_ref
        )
        existing = [d for d in await self.list_usage(subject) if d.feature is feature]
        if not existing:
            try:
                async with self._session.begin_nested():
                    self._session.add(
                        QualityUsageDeclarationRow(
                            id=uuid.uuid4(),
                            episode_id=_as_uuid(subject.episode_id),
                            final_video_sha256=subject.final_video_sha256,
                            input_hash=subject.input_hash,
                            feature=feature.value,
                            used=used,
                            declared_by=declared_by,
                            declared_at=declared_at,
                            evidence_ref=evidence_ref,
                        )
                    )
                return declaration
            except IntegrityError:
                existing = [d for d in await self.list_usage(subject) if d.feature is feature]
        if existing and existing[0].used == used and existing[0].subject == subject:
            return existing[0]
        raise CheckSetInvalid("a conflicting usage declaration already exists for this video")

    async def save_report(self, report: QualityReport) -> str:
        """組み立てた報告を保存して id を返す（拒否の報告も残す。INV-128）。"""
        payload = report.to_dict()
        row = QualityReportRow(
            id=uuid.uuid4(),
            episode_id=_as_uuid(report.episode_id),
            final_video_sha256=report.final_video_sha256,
            input_hash=report.input_hash,
            rule_version=report.rule_version,
            verdict=report.verdict.value,
            generator_version=report.generator_version,
            generated_at=report.generated_at,
            expires_at=report.expires_at,
            report_json=payload,
            report_sha256=sha256_hex(canonical_json_bytes(payload)),
        )
        self._session.add(row)
        await self._session.flush()
        return str(row.id)

    async def get_report_binding(self, report_id: str) -> tuple[str, str, str] | None:
        """``(episode_id, final_video_sha256, verdict)``。承認が対象の報告を確かめるのに使う。"""
        row = await self._session.get(QualityReportRow, _as_uuid(report_id))
        if row is None:
            return None
        return str(row.episode_id), row.final_video_sha256, row.verdict

    async def save_manual_approval(
        self,
        *,
        episode_id: str,
        report_id: str,
        final_video_sha256: str,
        approver_kind: ApproverKind,
        approver_id: str,
        approved_at: datetime,
    ) -> str:
        """REVIEW の手動承認を足す。報告と対象 hash が食い違えば DB の複合 FK が拒否する。"""
        row = QualityManualApprovalRow(
            id=uuid.uuid4(),
            episode_id=_as_uuid(episode_id),
            report_id=_as_uuid(report_id),
            final_video_sha256=final_video_sha256,
            approved_verdict=QualityVerdict.REVIEW.value,
            approver_kind=approver_kind.value,
            approver_id=approver_id,
            approved_at=approved_at,
        )
        self._session.add(row)
        await self._session.flush()
        return str(row.id)

    async def list_manual_approvals(
        self, episode_id: str, final_video_sha256: str
    ) -> Sequence[QualityManualApprovalRow]:
        stmt = select(QualityManualApprovalRow).where(
            QualityManualApprovalRow.episode_id == _as_uuid(episode_id),
            QualityManualApprovalRow.final_video_sha256 == final_video_sha256,
        )
        return (await self._session.execute(stmt)).scalars().all()


class SqlQualityRecordWriter:
    """``QualityRecordWriter`` の実装。呼び出しごとに自分の session で commit する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def write_check_set(
        self,
        subject: CheckSubject,
        draft: CheckSetDraft,
        *,
        checked_at: datetime,
        rule_version: str,
    ) -> CheckSetRecord:
        async with self._session_factory() as session:
            record = await QualityRecordRepository(session).write_check_set(
                subject, draft, checked_at=checked_at, rule_version=rule_version
            )
            await session.commit()
            return record

    async def declare_usage(
        self,
        subject: CheckSubject,
        feature: UsageFeature,
        *,
        used: bool,
        declared_by: str,
        declared_at: datetime,
        evidence_ref: str | None = None,
    ) -> UsageDeclaration:
        async with self._session_factory() as session:
            declaration = await QualityRecordRepository(session).declare_usage(
                subject,
                feature,
                used=used,
                declared_by=declared_by,
                declared_at=declared_at,
                evidence_ref=evidence_ref,
            )
            await session.commit()
            return declaration
