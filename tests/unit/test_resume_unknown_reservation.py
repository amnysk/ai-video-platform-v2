"""再開しても UNKNOWN の予約から二重 submit しない（ADR-0053 / INV-67 / INV-15）。

本番の sb4 の形: fal の結果取得が 403 で、予約は ``reserved`` + provider job 参照ありのまま。
理由は docs/testing/reservation-reconciliation-rationale.md。fake のみ。
"""

from __future__ import annotations

import pytest

from contracts.states import ArtifactType, ProviderCall, ReservationStatus
from domain.errors import ProviderUnavailableError, ResubmitRejectedError
from domain.production.ports import ImageRequest, JobStatus, ProviderJobRef
from domain.provider.reconciliation import validate_manual_resolution
from infrastructure.db.repositories import (
    EpisodeRepository,
    ProviderReconciliationRepository,
    ProviderReservationRepository,
)
from infrastructure.production.paid_job import PaidJobRunner, PaidJobSpec, Submitted
from infrastructure.workdir import WorkDirectory
from tests.support.production import ENABLED_BUDGET, FakeImageGenerator

REQUEST = ImageRequest(prompt="p", width=1080, height=1920, aspect="9:16")


class _Result403Generator(FakeImageGenerator):
    """結果取得が 403（資格情報）で拒否される fal の形。"""

    async def poll(self, ref: ProviderJobRef) -> JobStatus:
        raise ProviderUnavailableError(
            "fal result refused: HTTP 403 (credentials)", http_status=403
        )


@pytest.fixture
def runner(session_factory, artifact_store, tmp_path) -> PaidJobRunner:
    return PaidJobRunner(
        budget=ENABLED_BUDGET,
        session_factory=session_factory,
        store=artifact_store,
        workdir=WorkDirectory(tmp_path / "work", forbidden=()),
    )


async def _blocked_on_403(runner, session_factory):
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
    spec = PaidJobSpec(
        episode_id=episode.id,
        scene_id="sb4",
        provider=ProviderCall.FAL_IMAGE,
        artifact_type=ArtifactType.SCENE_IMAGE,
        input_hash="h" * 64,
        round=1,
    )
    gen = _Result403Generator()
    first = await runner.submit(spec, gen, REQUEST)
    assert isinstance(first, Submitted)
    with pytest.raises(ProviderUnavailableError):
        await runner.await_output(first.reservation_id, gen, poll_interval_seconds=0)
    async with session_factory() as session:
        row = await ProviderReservationRepository(session).get(first.reservation_id)
    assert row is not None and row.status is ReservationStatus.RESERVED  # 宙吊りのまま
    return spec, gen, first.reservation_id


async def test_a_403_on_the_result_leaves_the_reservation_reserved_and_resume_awaits_it(
    runner, session_factory
) -> None:
    spec, gen, rid = await _blocked_on_403(runner, session_factory)
    again = await runner.submit(spec, gen, REQUEST)  # 運用者の再開
    assert isinstance(again, Submitted) and not again.newly_submitted
    assert again.reservation_id == rid
    assert gen.submit_calls == 1  # 再 submit していない


async def test_after_manual_success_resume_cannot_submit_again(runner, session_factory) -> None:
    spec, gen, rid = await _blocked_on_403(runner, session_factory)
    resolution = validate_manual_resolution(
        outcome="success", evidence="dashboard", owner_approval="OWNER-20261021-A1B2C3", actor="o"
    )
    async with session_factory() as session:
        await ProviderReconciliationRepository(session).resolve_manual(rid, resolution)
        await session.commit()
    with pytest.raises(ResubmitRejectedError):
        await runner.submit(spec, gen, REQUEST)
    assert gen.submit_calls == 1


async def test_after_manual_failure_resume_opens_the_next_round(runner, session_factory) -> None:
    """失敗確定は意図して次ラウンドを許す（課金済みとして spent。運用者が再生成を承認した扱い）。"""
    spec, gen, rid = await _blocked_on_403(runner, session_factory)
    resolution = validate_manual_resolution(
        outcome="failure", evidence="dashboard", owner_approval="OWNER-20261021-A1B2C3", actor="o"
    )
    async with session_factory() as session:
        await ProviderReconciliationRepository(session).resolve_manual(rid, resolution)
        await session.commit()
    again = await runner.submit(spec, gen, REQUEST)
    assert isinstance(again, Submitted) and again.newly_submitted and again.round == 2
    assert gen.submit_calls == 2
