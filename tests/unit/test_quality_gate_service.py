"""検査記録に基づく品質ゲート（ADR-0081 / INV-126〜130）。実の render で作った final_video を使う。

テストの理由は docs/testing/v3-phase3-quality.md。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from contracts.approval import ApproverKind
from contracts.artifacts import parse_final_video
from contracts.quality import (
    MAX_REPORT_VALIDITY,
    QUALITY_RULE_VERSION,
    QualityCheck,
    QualityGate,
    QualityReason,
    QualityVerdict,
)
from contracts.quality_records import CHECK_KEY_GATES, CheckKey, UsageFeature
from contracts.states import ArtifactType
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.quality.approval import UploadApprover
from domain.quality.records import CheckSetDraft, CheckSubject
from infrastructure.db.models import (
    ArtifactMetadataRow,
    Base,
    QualityCheckRecordRow,
    QualityManualApprovalRow,
    QualityReportRow,
)
from infrastructure.db.quality_repositories import QualityRecordRepository, SqlQualityRecordWriter
from infrastructure.db.repositories import ArtifactMetadataRepository
from infrastructure.quality.service import QualityGateService
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.upload import seed_render_ready

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def factory(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'quality.db'}",
        poolclass=NullPool,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class FakeProducer:
    """BGM / 地図の枝が差し込む producer の代役。"""

    def __init__(self, key: CheckKey, verdict: QualityVerdict = QualityVerdict.PASS) -> None:
        self.key = key
        self.verdict = verdict
        self.calls = 0
        self.fail = False

    async def produce(self, context: Any, *, now: datetime) -> CheckSetDraft | None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("producer down")
        ok = self.verdict is QualityVerdict.PASS
        checks = tuple(
            QualityCheck(
                gate=gate,
                verdict=self.verdict,
                reason_code=QualityReason.OK if ok else QualityReason.LICENSE_UNVERIFIED,
                detail="fake producer",
                deterministic=True,
            )
            for gate in CHECK_KEY_GATES[self.key]
        )
        return CheckSetDraft(self.key, checks, "fake-producer", "1")


class Env:
    def __init__(self, factory, store, tmp_path: Path) -> None:
        self.factory = factory
        self.store = store
        self.tmp_path = tmp_path
        self.clock = Clock()
        self.episode_id = ""
        self.subject: CheckSubject

    def service(self, *producers: FakeProducer) -> QualityGateService:
        return QualityGateService(
            session_factory=self.factory,
            store=self.store,
            producers=producers,
            destination_id="UC" + "a" * 22,
            clock=self.clock,
        )

    async def declare(self, bgm: bool, map_: bool) -> None:
        writer = SqlQualityRecordWriter(self.factory)
        for feature, used in ((UsageFeature.BGM, bgm), (UsageFeature.MAP, map_)):
            await writer.declare_usage(
                self.subject,
                feature,
                used=used,
                declared_by="render",
                declared_at=self.clock.now,
                evidence_ref="evidence-1" if used else None,
            )

    async def rows(self, model):
        async with self.factory() as s:
            return (await s.execute(select(model))).scalars().all()


@pytest_asyncio.fixture
async def env(factory, tmp_path: Path):
    store = InMemoryArtifactStore()
    e = Env(factory, store, tmp_path)
    e.episode_id = await seed_render_ready(factory, store, tmp_path)
    async with factory() as s:
        current = await QualityRecordRepository(s).current_final_video(e.episode_id)
    assert current is not None
    e.subject = CheckSubject(e.episode_id, current.final_video_sha256, current.input_hash)
    return e


async def _auto(env: Env, *producers: FakeProducer):
    return await env.service(*producers).evaluate(env.episode_id, manual=False)


# --- 正常系と必須の集合 --------------------------------------------------------------------


async def test_with_declared_non_use_the_builtin_checks_alone_admit_the_episode(env: Env) -> None:
    await env.declare(bgm=False, map_=False)
    outcome = await _auto(env)
    assert outcome.assessment.decision.allowed, outcome.assessment
    assert outcome.assessment.verdict is QualityVerdict.PASS
    records = await env.rows(QualityCheckRecordRow)
    assert {r.check_key for r in records} == {"technical_qa", "description_lint"}
    assert all(r.final_video_sha256 == env.subject.final_video_sha256 for r in records)
    assert all(r.rule_version == QUALITY_RULE_VERSION for r in records)


async def test_without_usage_declarations_the_gate_refuses_as_review(env: Env) -> None:
    outcome = await _auto(env)
    assert not outcome.assessment.decision.allowed
    assert outcome.assessment.verdict is QualityVerdict.REVIEW
    assert outcome.assessment.decision.reason_code is QualityReason.REPORT_NEEDS_REVIEW


async def test_the_report_is_persisted_bound_to_the_video_hash_even_when_refused(
    env: Env,
) -> None:
    outcome = await _auto(env)
    (row,) = await env.rows(QualityReportRow)
    assert str(row.id) == outcome.report_id
    assert row.final_video_sha256 == env.subject.final_video_sha256
    assert row.input_hash == env.subject.input_hash
    assert row.rule_version == QUALITY_RULE_VERSION
    assert row.verdict == "review"
    reasons = {c["reason_code"] for c in row.report_json["checks"]}
    assert QualityReason.USAGE_DECLARATION_MISSING.value in reasons


async def test_a_used_feature_is_required_and_a_producer_supplies_it(env: Env) -> None:
    await env.declare(bgm=True, map_=False)
    assert not (await _auto(env)).assessment.decision.allowed  # producer が無い
    producer = FakeProducer(CheckKey.BGM)
    outcome = await _auto(env, producer)
    assert outcome.assessment.decision.allowed
    assert producer.calls == 1
    again = await _auto(env, producer)
    assert again.assessment.decision.allowed
    assert producer.calls == 1  # 有効な記録は再利用する（再生成しない）


async def test_a_failing_producer_leaves_the_set_missing_and_the_gate_refuses(env: Env) -> None:
    await env.declare(bgm=True, map_=False)
    producer = FakeProducer(CheckKey.BGM)
    producer.fail = True
    outcome = await _auto(env, producer)
    assert not outcome.assessment.decision.allowed
    assert outcome.assessment.verdict is QualityVerdict.REVIEW


async def test_a_producer_block_is_a_block(env: Env) -> None:
    await env.declare(bgm=True, map_=False)
    outcome = await _auto(env, FakeProducer(CheckKey.BGM, QualityVerdict.BLOCK))
    assert outcome.assessment.verdict is QualityVerdict.BLOCK
    assert not outcome.assessment.decision.allowed


# --- 古い記録は数えない（INV-126）---------------------------------------------------------


async def test_expired_records_are_not_reused_and_are_regenerated(env: Env) -> None:
    await env.declare(bgm=False, map_=False)
    assert (await _auto(env)).assessment.decision.allowed
    env.clock.now = NOW + MAX_REPORT_VALIDITY + timedelta(minutes=1)
    outcome = await _auto(env)
    assert outcome.assessment.decision.allowed
    generations = sorted(r.generation for r in await env.rows(QualityCheckRecordRow))
    assert generations == [1, 1, 2, 2]  # 期限切れは書き換えず、新しい世代を足す


async def test_an_expired_record_without_a_producer_is_refused(env: Env) -> None:
    await env.declare(bgm=True, map_=False)
    producer = FakeProducer(CheckKey.BGM)
    assert (await _auto(env, producer)).assessment.decision.allowed
    env.clock.now = NOW + MAX_REPORT_VALIDITY + timedelta(minutes=1)
    assert not (await _auto(env)).assessment.decision.allowed  # BGM の producer が無い


async def test_a_record_with_an_older_rule_version_is_not_counted(env: Env) -> None:
    await env.declare(bgm=True, map_=False)
    draft = await FakeProducer(CheckKey.BGM).produce(None, now=NOW)
    assert draft is not None
    await SqlQualityRecordWriter(env.factory).write_check_set(
        env.subject, draft, checked_at=NOW, rule_version="deterministic-1"
    )
    assert not (await _auto(env)).assessment.decision.allowed


async def test_a_record_for_another_video_hash_is_not_counted(env: Env) -> None:
    await env.declare(bgm=True, map_=False)
    draft = await FakeProducer(CheckKey.BGM).produce(None, now=NOW)
    assert draft is not None
    other = CheckSubject(env.episode_id, "f" * 64, env.subject.input_hash)
    await SqlQualityRecordWriter(env.factory).write_check_set(
        other, draft, checked_at=NOW, rule_version=QUALITY_RULE_VERSION
    )
    assert not (await _auto(env)).assessment.decision.allowed


async def test_a_new_final_video_gets_none_of_the_old_records_or_declarations(env: Env) -> None:
    await env.declare(bgm=False, map_=False)
    assert (await _auto(env)).assessment.decision.allowed
    async with env.factory() as s:  # 再 render の模擬: 旧行を世代交代し、別 sha の現行行を足す
        await s.execute(
            update(ArtifactMetadataRow)
            .where(
                ArtifactMetadataRow.episode_id == uuid.UUID(env.episode_id),
                ArtifactMetadataRow.artifact_type == ArtifactType.FINAL_VIDEO.value,
            )
            .values(superseded_at=NOW)
        )
        await ArtifactMetadataRepository(s).record(
            episode_id=env.episode_id,
            artifact_type=ArtifactType.FINAL_VIDEO,
            schema_version="1.0",
            bucket="artifacts",
            object_key="episodes/x/missing.json",
            sha256="9" * 64,
            input_hash="8" * 64,
        )
        await s.commit()
    outcome = await _auto(env)
    assert not outcome.assessment.decision.allowed
    assert outcome.final_video_sha256 == "9" * 64


async def test_a_tampered_final_video_body_is_never_trusted(env: Env) -> None:
    await env.declare(bgm=False, map_=False)
    async with env.factory() as s:
        current = await QualityRecordRepository(s).current_final_video(env.episode_id)
    assert current is not None
    body = await env.store.get_json(current.object_key)
    body["total_duration_ms"] += 1  # 保存された sha256 と合わなくなる
    env.store._objects[current.object_key] = canonical_json_bytes(body)  # 保存物の差し替え
    outcome = await _auto(env)
    assert not outcome.assessment.decision.allowed
    assert await env.rows(QualityCheckRecordRow) == []


async def test_a_technical_qa_failure_in_the_stored_video_blocks(env: Env) -> None:
    await env.declare(bgm=False, map_=False)
    async with env.factory() as s:
        current = await QualityRecordRepository(s).current_final_video(env.episode_id)
    assert current is not None
    body = await env.store.get_json(current.object_key)
    body["technical_qa"]["checks"] = body["technical_qa"]["checks"][:1]  # 空洞化
    digest = sha256_hex(canonical_json_bytes(body))
    key = f"{current.object_key}.hollow"
    await env.store.put_json(key, body)
    async with env.factory() as s:
        await s.execute(
            update(ArtifactMetadataRow)
            .where(ArtifactMetadataRow.id == uuid.UUID(current.artifact_id))
            .values(sha256=digest, object_key=key)
        )
        await s.commit()
    outcome = await _auto(env)
    assert outcome.assessment.verdict is QualityVerdict.BLOCK
    assert not outcome.assessment.decision.allowed


async def test_the_stored_final_video_parses_with_its_technical_qa(env: Env) -> None:
    async with env.factory() as s:
        current = await QualityRecordRepository(s).current_final_video(env.episode_id)
    assert current is not None
    final = parse_final_video(await env.store.get_json(current.object_key))
    assert final.technical_qa.passed


# --- 保存の失敗・例外は PASS に倒れない -------------------------------------------------


async def test_when_the_report_cannot_be_saved_the_upload_is_refused(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.declare(bgm=False, map_=False)

    async def boom(self, report):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")

    monkeypatch.setattr(QualityRecordRepository, "save_report", boom)
    outcome = await _auto(env)
    assert not outcome.assessment.decision.allowed
    assert outcome.assessment.decision.reason_code is QualityReason.REPORT_PRODUCTION_FAILED
    assert outcome.report_id is None


async def test_an_unexpected_error_is_a_refusal_not_an_exception(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self, subject, now):  # type: ignore[no-untyped-def]
        raise RuntimeError("unexpected")

    monkeypatch.setattr(QualityGateService, "_load", boom)
    outcome = await _auto(env)
    assert outcome.assessment.decision.reason_code is QualityReason.REPORT_EVALUATION_ERROR
    assert not outcome.assessment.decision.allowed


async def test_an_episode_without_a_final_video_is_refused(factory, tmp_path: Path) -> None:
    from infrastructure.db.repositories import EpisodeRepository

    async with factory() as s:
        ep = await EpisodeRepository(s).create(topic="t")
        await s.commit()
    service = QualityGateService(
        session_factory=factory, store=InMemoryArtifactStore(), clock=Clock()
    )
    outcome = await service.evaluate(ep.id, manual=False)
    assert not outcome.assessment.decision.allowed


# --- 手動承認（INV-129）------------------------------------------------------------------


async def test_manual_review_is_allowed_to_proceed_and_an_owner_approval_is_recorded(
    env: Env,
) -> None:
    service = env.service()
    outcome = await service.evaluate(env.episode_id, manual=True)
    assert outcome.assessment.decision.allowed and outcome.assessment.review_approved
    approval_id = await service.record_manual_approval(
        outcome, episode_id=env.episode_id, approver=UploadApprover(ApproverKind.OWNER, "owner-1")
    )
    (row,) = await env.rows(QualityManualApprovalRow)
    assert str(row.id) == approval_id
    assert (row.approver_kind, row.approver_id) == ("owner", "owner-1")
    assert row.final_video_sha256 == env.subject.final_video_sha256
    assert str(row.report_id) == outcome.report_id
    assert row.approved_at is not None


@pytest.mark.parametrize(
    "approver",
    [
        UploadApprover(ApproverKind.AGENT_DELEGATED, "agent-1"),
        None,
        UploadApprover(ApproverKind.OWNER, ""),
        UploadApprover(ApproverKind.OWNER, "has space"),
    ],
    ids=["agent_delegated", "unknown", "empty_id", "unsafe_id"],
)
async def test_only_a_known_owner_can_have_an_approval_recorded(
    env: Env, approver: UploadApprover | None
) -> None:
    service = env.service()
    outcome = await service.evaluate(env.episode_id, manual=True)
    with pytest.raises(PermissionError):
        await service.record_manual_approval(
            outcome,
            episode_id=env.episode_id,
            approver=approver,  # type: ignore[arg-type]
        )
    assert await env.rows(QualityManualApprovalRow) == []


async def test_manual_block_is_never_allowed(env: Env) -> None:
    await env.declare(bgm=True, map_=False)
    outcome = await env.service(FakeProducer(CheckKey.BGM, QualityVerdict.BLOCK)).evaluate(
        env.episode_id, manual=True
    )
    assert not outcome.assessment.decision.allowed


async def test_gates_cover_every_check_key(env: Env) -> None:
    assert {g for gates in CHECK_KEY_GATES.values() for g in gates} == {
        QualityGate.VISUAL,
        QualityGate.AUDIO,
        QualityGate.METADATA,
        QualityGate.MUSIC,
        QualityGate.LICENSE,
        QualityGate.GEO,
    }
