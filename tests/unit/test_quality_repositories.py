"""品質検査の永続化（SQLite。ADR-0081 / INV-126〜129）。

PostgreSQL 固有の保証（UPDATE 拒否トリガ・複合 FK・24h CHECK）は
tests/integration/test_quality_records_postgres.py で固定する。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from contracts.approval import ApproverKind
from contracts.quality import (
    MAX_REPORT_VALIDITY,
    QualityCheck,
    QualityReason,
    QualityReport,
    QualityVerdict,
)
from contracts.quality_records import CHECK_KEY_GATES, CheckKey, UsageFeature
from domain.episode.entities import Episode  # noqa: F401
from domain.quality.records import (
    CheckSetDraft,
    CheckSetInvalid,
    CheckSubject,
    select_fresh_check_sets,
    select_usage,
)
from infrastructure.db.quality_repositories import QualityRecordRepository, SqlQualityRecordWriter
from infrastructure.db.repositories import EpisodeRepository

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
RULE = "deterministic-test"
SHA = "a" * 64
IHASH = "c" * 64


def _draft(
    key: CheckKey = CheckKey.MAP, verdict: QualityVerdict = QualityVerdict.PASS
) -> CheckSetDraft:
    ok = verdict is QualityVerdict.PASS
    checks = tuple(
        QualityCheck(
            gate=g,
            verdict=verdict,
            reason_code=QualityReason.OK if ok else QualityReason.GEO_MISMATCH,
            detail="fixture",
            deterministic=True,
            evidence_ref="evidence-1",
        )
        for g in CHECK_KEY_GATES[key]
    )
    return CheckSetDraft(key, checks, "fixture", "1", "evidence-1")


async def _subject(session_factory) -> CheckSubject:
    async with session_factory() as s:
        ep = await EpisodeRepository(s).create(topic="t")
        await s.commit()
    return CheckSubject(ep.id, SHA, IHASH)


async def test_a_written_set_round_trips_and_is_fresh(session_factory) -> None:
    subject = await _subject(session_factory)
    writer = SqlQualityRecordWriter(session_factory)
    written = await writer.write_check_set(subject, _draft(), checked_at=NOW, rule_version=RULE)
    async with session_factory() as s:
        rows = await QualityRecordRepository(s).list_check_sets(subject)
    assert [r.id for r in rows] == [written.id]
    assert rows[0].checks == written.checks
    assert rows[0].expires_at - rows[0].checked_at == MAX_REPORT_VALIDITY
    fresh = select_fresh_check_sets(rows, subject, rule_version=RULE, now=NOW)
    assert CheckKey.MAP in fresh.fresh


async def test_rechecking_adds_a_new_generation_and_never_rewrites(session_factory) -> None:
    subject = await _subject(session_factory)
    writer = SqlQualityRecordWriter(session_factory)
    first = await writer.write_check_set(subject, _draft(), checked_at=NOW, rule_version=RULE)
    second = await writer.write_check_set(
        subject,
        _draft(verdict=QualityVerdict.BLOCK),
        checked_at=NOW + timedelta(minutes=1),
        rule_version=RULE,
    )
    other_rule = await writer.write_check_set(subject, _draft(), checked_at=NOW, rule_version="r2")
    assert (first.generation, second.generation, other_rule.generation) == (1, 2, 1)
    async with session_factory() as s:
        rows = await QualityRecordRepository(s).list_check_sets(subject)
    assert len(rows) == 3
    fresh = select_fresh_check_sets(
        rows, subject, rule_version=RULE, now=NOW + timedelta(minutes=2)
    )
    assert fresh.fresh[CheckKey.MAP].verdict is QualityVerdict.BLOCK


async def test_reads_are_scoped_to_the_subject_hash(session_factory) -> None:
    subject = await _subject(session_factory)
    other = CheckSubject(subject.episode_id, "b" * 64, IHASH)
    writer = SqlQualityRecordWriter(session_factory)
    await writer.write_check_set(other, _draft(), checked_at=NOW, rule_version=RULE)
    async with session_factory() as s:
        assert await QualityRecordRepository(s).list_check_sets(subject) == []


async def test_a_corrupt_row_is_treated_as_missing_not_as_pass(session_factory) -> None:
    subject = await _subject(session_factory)
    writer = SqlQualityRecordWriter(session_factory)
    written = await writer.write_check_set(subject, _draft(), checked_at=NOW, rule_version=RULE)
    from sqlalchemy import update

    from infrastructure.db.models import QualityCheckRecordRow

    async with session_factory() as s:
        await s.execute(
            update(QualityCheckRecordRow)
            .where(QualityCheckRecordRow.id == uuid.UUID(written.id))
            .values(checks=[{"gate": "nonsense"}])
        )
        await s.commit()
        assert await QualityRecordRepository(s).list_check_sets(subject) == []


async def test_usage_declaration_is_idempotent_and_rejects_a_contradiction(session_factory) -> None:
    subject = await _subject(session_factory)
    writer = SqlQualityRecordWriter(session_factory)
    kw = {"declared_by": "render", "declared_at": NOW}
    first = await writer.declare_usage(subject, UsageFeature.BGM, used=False, **kw)
    again = await writer.declare_usage(subject, UsageFeature.BGM, used=False, **kw)
    assert first == again
    with pytest.raises(CheckSetInvalid):
        await writer.declare_usage(
            subject, UsageFeature.BGM, used=True, evidence_ref="evidence-1", **kw
        )
    async with session_factory() as s:
        usage = select_usage(await QualityRecordRepository(s).list_usage(subject), subject)
    assert usage[UsageFeature.BGM].used is False


async def test_a_used_declaration_without_evidence_cannot_be_written(session_factory) -> None:
    subject = await _subject(session_factory)
    with pytest.raises(CheckSetInvalid):
        await SqlQualityRecordWriter(session_factory).declare_usage(
            subject, UsageFeature.MAP, used=True, declared_by="render", declared_at=NOW
        )


def _report(subject: CheckSubject, verdict: QualityVerdict) -> QualityReport:
    return QualityReport(
        episode_id=subject.episode_id,
        final_video_sha256=subject.final_video_sha256,
        input_hash=subject.input_hash,
        generated_at=NOW,
        generator_version="g",
        reviewer_version="none",
        rule_version=RULE,
        expires_at=NOW + MAX_REPORT_VALIDITY,
        verdict=verdict,
        checks=_draft().checks,
    )


async def test_report_and_approval_are_stored_with_who_when_and_which_hash(session_factory) -> None:
    subject = await _subject(session_factory)
    async with session_factory() as s:
        repo = QualityRecordRepository(s)
        report_id = await repo.save_report(_report(subject, QualityVerdict.REVIEW))
        approval_id = await repo.save_manual_approval(
            episode_id=subject.episode_id,
            report_id=report_id,
            final_video_sha256=SHA,
            approver_kind=ApproverKind.OWNER,
            approver_id="owner-1",
            approved_at=NOW,
        )
        await s.commit()
    async with session_factory() as s:
        repo = QualityRecordRepository(s)
        assert await repo.get_report_binding(report_id) == (subject.episode_id, SHA, "review")
        rows = await repo.list_manual_approvals(subject.episode_id, SHA)
    assert [str(r.id) for r in rows] == [approval_id]
    assert rows[0].approver_kind == "owner" and rows[0].approver_id == "owner-1"
    assert str(rows[0].report_id) == report_id


async def test_the_stored_report_is_the_exact_serialisation(session_factory) -> None:
    subject = await _subject(session_factory)
    report = _report(subject, QualityVerdict.PASS)
    async with session_factory() as s:
        report_id = await QualityRecordRepository(s).save_report(report)
        await s.commit()
    from infrastructure.db.models import QualityReportRow

    async with session_factory() as s:
        row = await s.get(QualityReportRow, uuid.UUID(report_id))
    assert row is not None and row.report_json == report.to_dict()
    assert row.final_video_sha256 == SHA and row.rule_version == RULE
