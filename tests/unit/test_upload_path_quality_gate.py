"""全ての投稿経路は、YouTube を呼ぶ前に同じ品質 gate を通る（ADR-0087 §9 / INV-158）。

経路の表は docs/testing/bgm-auto-safety-rationale.md。ここでは「UploadWorkflow の直接起動・Temporal
CLI・Activity の再試行・再投稿」を代表する ``upload_final_video`` を直接呼び、ENFORCE が ON なら
PASS / WARN 以外で YouTube の fake が 1 回も呼ばれない（session 0・動画 0・予約 0）ことを検査する。
OFF は導入前と同一（INV-130: サービスに触れず、品質の表に書かない）。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from temporalio.exceptions import ApplicationError

from contracts.approval import ApproverKind
from contracts.quality import MAX_REPORT_VALIDITY, QualityReason, QualityVerdict
from contracts.quality_records import CheckKey
from contracts.states import ProviderCall
from contracts.upload_activities import UploadAdmitRequest, UploadFinalVideoRequest
from domain.quality.approval import UploadApprover
from domain.quality.deterministic import QualityAssessment
from domain.quality.upload_decision import UploadDecision
from infrastructure.db.models import (
    ProviderReservationRow,
    QualityCheckRecordRow,
    QualityManualApprovalRow,
    QualityReportRow,
    QualityUsageDeclarationRow,
)
from infrastructure.db.quality_repositories import QualityRecordRepository
from infrastructure.db.repositories import ProviderReservationRepository
from infrastructure.quality.service import GateOutcome, QualityGateService
from infrastructure.workdir import WorkDirectory
from tests.support.fake_youtube import FakeVideoUploader
from tests.support.upload import CHANNEL_ID, TEST_CHUNK_BYTES, seed_render_ready
from tests.unit import test_quality_gate_persisted_wiring as _wiring
from tests.unit.test_quality_gate_persisted_wiring import NOW, ExplodingService, World
from tests.unit.test_quality_gate_service import Clock, FakeProducer
from workers.upload.activities import UploadActivities

# 共有の fixture（SQLite のファイル DB・render_ready の Episode）。名前を再束縛して pytest に見せる
factory = _wiring.factory
world = _wiring.world

WF = "episode-x-upload"
OWNER = UploadApprover(ApproverKind.OWNER, "owner-1")
AGENT = UploadApprover(ApproverKind.AGENT_DELEGATED, "agent-1")


class Rig:
    """UploadActivities と YouTube の fake。呼び出しの回数は ``fake`` が数える。"""

    def __init__(self, world: World, tmp_path: Path) -> None:
        self.world = world
        self.tmp_path = tmp_path
        self.fake = FakeVideoUploader(chunk_bytes=TEST_CHUNK_BYTES)

    def acts(self, *, enforce: bool, service: Any, uploader: Any = None) -> UploadActivities:
        acts = UploadActivities(
            session_factory=self.world.factory,
            store=self.world.store,
            bucket="artifacts",
            workdir=WorkDirectory(self.tmp_path / "work", forbidden=()),
            uploader=uploader or self.fake,
            channel_id=CHANNEL_ID,
            chunk_bytes=TEST_CHUNK_BYTES,
            marker_lookup_attempts=2,
            marker_lookup_delay_seconds=0.0,
            transient_backoff_seconds=0.0,
            expiry_confirm_delay_seconds=0.0,
            heartbeat=lambda *_: None,
            quality_enforce=enforce,
            quality_service=service,
        )
        acts.heartbeat_interval_seconds = 0.05
        return acts

    async def admit(self, acts: UploadActivities, run: str = "run-1"):
        return await acts.admit(UploadAdmitRequest(self.world.episode_id, WF, run))

    async def upload(self, acts: UploadActivities, run: str = "run-1"):
        return await acts.upload_final_video(
            UploadFinalVideoRequest(self.world.episode_id, WF, run)
        )

    async def reservations(self):
        async with self.world.factory() as s:
            return await ProviderReservationRepository(s).list_for_episode_provider(
                self.world.episode_id, ProviderCall.YOUTUBE_UPLOAD
            )

    def youtube_calls(self) -> int:
        f = self.fake
        return f.sessions_started + f.chunk_sends + f.status_queries + f.videos_created


@pytest.fixture
def rig(world: World, tmp_path: Path) -> Rig:
    return Rig(world, tmp_path)


async def _refused(rig: Rig, acts: UploadActivities) -> ApplicationError:
    assert (await rig.admit(acts)).admitted
    with pytest.raises(ApplicationError) as info:
        await rig.upload(acts)
    return info.value


class StubService:
    """``QualityGateService.evaluate`` の代役。決めた判定を返し、呼び出しを数える。"""

    def __init__(
        self,
        allowed: bool,
        reason: QualityReason,
        *,
        review_approved: bool = False,
        sha: str | None = None,
    ) -> None:
        self.outcome = GateOutcome(
            QualityAssessment(UploadDecision(allowed, reason), None, review_approved), sha, None
        )
        self.calls: list[bool] = []

    async def evaluate(self, episode_id: str, *, manual: bool) -> GateOutcome:
        self.calls.append(manual)
        return self.outcome


# --- 拒否: YouTube を 1 回も呼ばない ---------------------------------------------------------

REFUSALS = [
    QualityReason.REPORT_BLOCKED,
    QualityReason.REPORT_NEEDS_REVIEW,
    QualityReason.REPORT_PRODUCTION_FAILED,
    QualityReason.REPORT_EVALUATION_ERROR,
    QualityReason.REPORT_EXPIRED,
    QualityReason.REQUIRED_CHECK_MISSING,
    QualityReason.USAGE_DECLARATION_MISSING,
]


@pytest.mark.parametrize("reason", REFUSALS, ids=lambda r: r.value)
async def test_a_refused_decision_never_reaches_youtube(rig: Rig, reason: QualityReason) -> None:
    acts = rig.acts(enforce=True, service=StubService(False, reason))
    err = await _refused(rig, acts)
    assert err.type == "UploadQualityRefusedError" and err.non_retryable
    assert reason.value in str(err)
    assert rig.youtube_calls() == 0 and rig.fake.videos_created == 0
    assert await rig.reservations() == []  # 予約も作らない


async def test_a_service_that_raises_is_a_refusal_not_a_pass(rig: Rig) -> None:
    ExplodingService.calls = 0
    err = await _refused(rig, rig.acts(enforce=True, service=ExplodingService()))
    assert err.type == "UploadQualityRefusedError"
    assert QualityReason.REPORT_EVALUATION_ERROR.value in str(err)
    assert rig.youtube_calls() == 0


async def test_enforce_on_without_a_service_fails_closed(rig: Rig) -> None:
    err = await _refused(rig, rig.acts(enforce=True, service=None))
    assert err.type == "UploadQualityRefusedError"
    assert rig.youtube_calls() == 0


async def test_the_real_gate_without_usage_declarations_is_a_review_and_refuses(
    rig: Rig, world: World
) -> None:
    """宣言が無い（= REVIEW）動画は、自動の gate を通らず直接起動されても投稿されない。"""
    err = await _refused(rig, rig.acts(enforce=True, service=world.service()))
    assert err.type == "UploadQualityRefusedError"
    assert rig.youtube_calls() == 0
    assert await world.count(QualityManualApprovalRow) == 0


async def test_a_producer_block_never_reaches_youtube_even_with_an_owner_approval(
    rig: Rig, world: World
) -> None:
    """BLOCK は承認で解放できない（INV-129）。承認の行があっても YouTube は 0 回。"""
    await world.declare(bgm=True)
    service = QualityGateService(
        session_factory=world.factory,
        store=world.store,
        producers=(FakeProducer(CheckKey.BGM, QualityVerdict.BLOCK),),
        clock=lambda: NOW,
    )
    outcome = await service.evaluate(world.episode_id, manual=True)
    assert not outcome.assessment.decision.allowed
    await _force_approval(world, OWNER)
    err = await _refused(rig, rig.acts(enforce=True, service=service))
    assert err.type == "UploadQualityRefusedError" and rig.youtube_calls() == 0


# --- 許可 -----------------------------------------------------------------------------------


async def test_a_pass_uploads_exactly_one_private_video(rig: Rig, world: World) -> None:
    await world.declare()
    acts = rig.acts(enforce=True, service=world.service())
    assert (await rig.admit(acts)).admitted
    result = await rig.upload(acts)
    assert result.video_id and rig.fake.videos_created == 1


async def test_a_warn_uploads(rig: Rig) -> None:
    stub = StubService(True, QualityReason.UPLOAD_ALLOWED_WITH_WARNINGS)
    acts = rig.acts(enforce=True, service=stub)
    assert (await rig.admit(acts)).admitted
    await rig.upload(acts)
    assert rig.fake.videos_created == 1 and stub.calls == [True]


# --- REVIEW は所有者の承認（INV-129）だけが解放する ---------------------------------------------


async def _force_approval(world: World, approver: UploadApprover) -> None:
    """API の手動経路が書く承認と同じ行を、同じ service の関数で書く。"""
    outcome = await world.service().evaluate(world.episode_id, manual=True)
    if approver.kind is ApproverKind.OWNER:
        await world.service().record_manual_approval(
            outcome, episode_id=world.episode_id, approver=approver
        )
    else:  # service は所有者以外の承認を拒む。DB 層に直接書かれた行でも解放されないことを見る
        await _write_row(world, outcome, approver)


async def _write_row(world: World, outcome: GateOutcome, approver: UploadApprover) -> None:
    async with world.factory() as s:
        await QualityRecordRepository(s).save_manual_approval(
            episode_id=world.episode_id,
            report_id=outcome.report_id or "",
            final_video_sha256=outcome.final_video_sha256 or "",
            approver_kind=approver.kind,
            approver_id=approver.approver_id,
            approved_at=NOW,
        )
        await s.commit()


async def test_a_review_with_the_owner_approval_recorded_proceeds(rig: Rig, world: World) -> None:
    await _force_approval(world, OWNER)
    acts = rig.acts(enforce=True, service=world.service())
    assert (await rig.admit(acts)).admitted
    result = await rig.upload(acts)
    assert result.video_id and rig.fake.videos_created == 1


async def test_a_review_with_only_a_delegated_agent_approval_is_refused(
    rig: Rig, world: World
) -> None:
    await _force_approval(world, AGENT)
    err = await _refused(rig, rig.acts(enforce=True, service=world.service()))
    assert err.type == "UploadQualityRefusedError" and rig.youtube_calls() == 0


async def test_an_approval_of_another_episode_does_not_release_this_one(
    rig: Rig, world: World, tmp_path: Path
) -> None:
    (tmp_path / "other").mkdir()
    other = await seed_render_ready(world.factory, world.store, tmp_path / "other")
    other_world = World(world.factory, world.store, other)
    async with world.factory() as s:
        current = await QualityRecordRepository(s).current_final_video(other)
    assert current is not None
    other_outcome = await other_world.service().evaluate(other, manual=True)
    await other_world.service().record_manual_approval(
        other_outcome, episode_id=other, approver=OWNER
    )
    err = await _refused(rig, rig.acts(enforce=True, service=world.service()))
    assert err.type == "UploadQualityRefusedError" and rig.youtube_calls() == 0


# --- OFF は導入前と同一（INV-130） ---------------------------------------------------------------


async def test_enforce_off_never_touches_the_service_or_the_quality_tables(
    rig: Rig, world: World
) -> None:
    ExplodingService.calls = 0
    acts = rig.acts(enforce=False, service=ExplodingService())
    assert (await rig.admit(acts)).admitted
    await rig.upload(acts)
    assert rig.fake.videos_created == 1 and ExplodingService.calls == 0
    for model in (
        QualityCheckRecordRow,
        QualityUsageDeclarationRow,
        QualityReportRow,
        QualityManualApprovalRow,
    ):
        assert await world.count(model) == 0


async def test_default_construction_is_the_pre_existing_behaviour(rig: Rig) -> None:
    acts = UploadActivities(
        session_factory=rig.world.factory,
        store=rig.world.store,
        bucket="artifacts",
        workdir=WorkDirectory(rig.tmp_path / "w2", forbidden=()),
        uploader=rig.fake,
        channel_id=CHANNEL_ID,
        chunk_bytes=TEST_CHUNK_BYTES,
        heartbeat=lambda *_: None,
    )
    assert (await rig.admit(acts)).admitted
    await rig.upload(acts)
    assert rig.fake.videos_created == 1


# --- 再投稿・鮮度（INV-126）---------------------------------------------------------------------


async def test_a_past_pass_is_reused_for_the_same_video_without_new_check_records(
    rig: Rig, world: World
) -> None:
    await world.declare()
    service = world.service()
    first = await service.evaluate(world.episode_id, manual=False)
    rows = await world.count(QualityCheckRecordRow)
    second = await service.evaluate(world.episode_id, manual=False)
    assert first.assessment.decision.allowed and second.assessment.decision.allowed
    assert await world.count(QualityCheckRecordRow) == rows  # 再利用: 検査を作り直さない


async def test_a_past_pass_is_refused_once_it_is_stale_and_cannot_be_regenerated(
    rig: Rig, world: World
) -> None:
    await world.declare(bgm=True)
    clock = Clock()
    producer = FakeProducer(CheckKey.BGM)

    def service() -> QualityGateService:
        return QualityGateService(
            session_factory=world.factory,
            store=world.store,
            producers=(producer,),
            clock=clock,
        )

    assert (await service().evaluate(world.episode_id, manual=False)).assessment.decision.allowed
    clock.now = NOW + MAX_REPORT_VALIDITY + timedelta(minutes=1)
    producer.fail = True  # 再検査できない
    acts = rig.acts(enforce=True, service=service())
    err = await _refused(rig, acts)
    assert err.type == "UploadQualityRefusedError" and rig.youtube_calls() == 0


async def test_a_past_pass_does_not_cover_a_changed_final_video(rig: Rig, world: World) -> None:
    """別の final_video（別 hash）は、前の PASS の記録を 1 つも持たない。"""
    await world.declare()
    assert (
        await world.service().evaluate(world.episode_id, manual=False)
    ).assessment.decision.allowed
    (rig.tmp_path / "o2").mkdir()
    other = await seed_render_ready(
        world.factory, world.store, rig.tmp_path / "o2", payload=b"a different video body"
    )
    outcome = await World(world.factory, world.store, other).service().evaluate(other, manual=False)
    assert not outcome.assessment.decision.allowed


async def test_the_gate_checks_never_create_a_provider_reservation_row(
    rig: Rig, world: World
) -> None:
    """gate の評価は課金台帳に触れない（refused でも通っても 0 行）。"""
    acts = rig.acts(enforce=True, service=world.service())
    await _refused(rig, acts)
    async with world.factory() as s:
        total = (
            await s.execute(select(func.count()).select_from(ProviderReservationRow))
        ).scalar_one()
    assert total == 0
