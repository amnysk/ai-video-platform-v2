"""``scripts/provider-credits.py``（ADR-0057 / INV-73）。SQLite + fake。fal・API へは接続しない。"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import uuid
from decimal import Decimal

import pytest

from contracts.pipeline import production_workflow_id
from contracts.provider_health import (
    RESUME_MAX_CONCURRENCY,
    BreakerState,
    HoldReason,
    ProviderAccount,
    ProviderFamily,
    RefusalKind,
)
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall
from domain.production.ports import ImageRequest
from infrastructure.db.models import EpisodeRow
from infrastructure.db.provider_health_repositories import PostgresProviderHealth
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository
from infrastructure.production.paid_job import PaidJobRunner, PaidJobSpec, Reused
from infrastructure.production.provider_credits import ResumeRejected, ResumeStarted
from infrastructure.workdir import WorkDirectory
from tests.support.production import FakeImageGenerator
from tests.unit.test_paid_job import _record_reusable_scene_image

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "provider-credits.py"
FAL = ProviderAccount(ProviderFamily.FAL)
TOKEN = "OWNER-20261025-SECRET123"
CAP = Decimal("50")


def _load():
    spec = importlib.util.spec_from_file_location("provider_credits_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResume:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def resume(self, episode_id: str) -> ResumeStarted:
        self.calls.append(episode_id)
        if self.fail:
            raise ResumeRejected("HTTP 409")
        return ResumeStarted(production_workflow_id(episode_id) + ":run2", "production")


@pytest.fixture
def health(session_factory) -> PostgresProviderHealth:
    return PostgresProviderHealth(session_factory)


async def _blocked_episode(session_factory, health, *, hold: bool = True) -> str:
    """balance 拒否で止まった production Episode（blocked + 保留）。"""
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        row = await session.get(EpisodeRow, uuid.UUID(episode.id))
        assert row is not None
        row.status = EpisodeStatus.BLOCKED.value
        row.workflow_id = production_workflow_id(episode.id) + ":run1"
        await session.commit()
    if hold:
        await health.hold_episode(
            episode_id=episode.id,
            scene_id="sb1",
            account=FAL,
            reason=HoldReason.WAITING_FOR_CREDITS,
            resumable_stage="production",
            generation_profile_id="p",
        )
    return episode.id


async def _open(health) -> None:
    await health.open_breaker(
        FAL, reason=RefusalKind.BALANCE_EXHAUSTED, http_status=403, evidence="x", episode_id=None
    )


async def _half_open(health) -> None:
    await _open(health)
    await health.half_open(
        FAL, approval_fingerprint="a" * 16, approved_by="owner", approval_evidence="topped up"
    )


def _resume_args(*ids: str, extra: list[str] | None = None, apply: bool = True) -> list[str]:
    argv = ["resume", "--owner-approval", TOKEN, "--evidence", "owner topped up 2026-10-25"]
    argv += ["--actor", "owner", "--estimated-additional-usd", "5"]
    for i in ids:
        argv += ["--episode-id", i]
    argv += extra or []
    return argv + (["--apply"] if apply else [])


async def _run(session_factory, argv, *, client=None, cap: Decimal | None = CAP):
    return await _load().run(
        argv, session_factory=session_factory, budget_cap=cap, resume_client=client
    )


async def test_status_is_read_only_and_reports_the_hold(session_factory, health) -> None:
    ep = await _blocked_episode(session_factory, health)
    await _open(health)
    code, out = await _run(session_factory, ["status"])
    assert code == 0
    assert out["breaker"]["state"] == "open" and out["breaker"]["reason"] == "balance_exhausted"
    assert [e["episode_id"] for e in out["held_episodes"]] == [ep]
    assert out["monthly_budget_usd"] == "50"
    assert (await health.snapshot(FAL)).state is BreakerState.OPEN  # 何も変えない


async def test_half_open_needs_a_structured_approval_and_evidence(session_factory, health) -> None:
    await _open(health)
    for argv in (
        ["half-open", "--actor", "o", "--evidence", "e"],
        ["half-open", "--owner-approval", "yes", "--actor", "o", "--evidence", "e"],
        ["half-open", "--owner-approval", TOKEN, "--actor", "o"],
    ):
        code, out = await _run(session_factory, argv + ["--apply"])
        assert code == 2 and "error" in out
    assert (await health.snapshot(FAL)).state is BreakerState.OPEN


async def test_half_open_is_dry_run_by_default_and_never_prints_the_token(
    session_factory, health
) -> None:
    await _open(health)
    argv = ["half-open", "--owner-approval", TOKEN, "--actor", "owner", "--evidence", "topped up"]
    code, out = await _run(session_factory, argv)
    assert code == 0 and out["dry_run"] is True
    assert (await health.snapshot(FAL)).state is BreakerState.OPEN
    code, out = await _run(session_factory, [*argv, "--apply"])
    assert code == 0 and out["changed"] is True
    assert (await health.snapshot(FAL)).state is BreakerState.HALF_OPEN
    assert "SECRET123" not in json.dumps(out, default=str)
    detail = await health.detail(FAL)
    assert detail is not None and detail.approval_fingerprint and TOKEN not in str(detail)


async def test_resume_is_refused_while_the_breaker_is_open(session_factory, health) -> None:
    ep = await _blocked_episode(session_factory, health)
    await _open(health)
    client = FakeResume()
    code, out = await _run(session_factory, _resume_args(ep), client=client)
    assert code == 2 and "breaker is open" in out["error"]
    assert client.calls == []


async def test_resume_is_refused_for_the_production_unreconciled_reservation_state(
    session_factory, health
) -> None:
    """本番の 591d1a8b… と同じ状態（fal_video・dispatched・参照あり・未照合）は再開を止める。"""
    ep = await _blocked_episode(session_factory, health)
    await _half_open(health)
    async with session_factory() as session:
        repo = ProviderReservationRepository(session)
        row = await repo.reserve(
            episode_id=ep,
            provider=ProviderCall.FAL_VIDEO,
            idempotency_key="591d1a8b" + "0" * 56,
            input_hash="h" * 64,
            round=1,
            scene_id="sb4",
            estimated_cost_usd=Decimal("0.97"),
        )
        await repo.mark_dispatched(row.id)
        await repo.record_provider_job_ref(row.id, '{"v":1,"request_id":"r"}')
        await session.commit()
    client = FakeResume()
    code, out = await _run(session_factory, _resume_args(ep), client=client)
    assert code == 2 and "unreconciled" in out["error"] and row.id in out["error"]
    assert client.calls == []
    assert await health.open_hold_count() == 1  # 保留は解放されない
    async with session_factory() as session:
        episode = await EpisodeRepository(session).get(ep)
        assert episode is not None and episode.status is EpisodeStatus.BLOCKED


async def test_resume_is_refused_over_budget_or_without_a_cap(session_factory, health) -> None:
    ep = await _blocked_episode(session_factory, health)
    await _half_open(health)
    client = FakeResume()
    code, out = await _run(session_factory, _resume_args(ep), client=client, cap=Decimal("4"))
    assert code == 2 and "budget would be exceeded" in out["error"]
    code, out = await _run(session_factory, _resume_args(ep), client=client, cap=None)
    assert code == 2 and "no monthly budget cap" in out["error"]
    assert client.calls == []


async def test_resume_concurrency_is_capped(session_factory, health) -> None:
    assert RESUME_MAX_CONCURRENCY == 1
    a = await _blocked_episode(session_factory, health)
    b = await _blocked_episode(session_factory, health)
    await _half_open(health)
    client = FakeResume()
    code, out = await _run(session_factory, _resume_args(a, b), client=client)
    assert code == 2 and "capped" in out["error"]
    code, out = await _run(
        session_factory,
        ["resume", "--owner-approval", TOKEN, "--evidence", "e", "--actor", "o"]
        + ["--estimated-additional-usd", "1", "--oldest", "2", "--apply"],
        client=client,
    )
    assert code == 2 and "capped" in out["error"]
    assert client.calls == []


async def test_oldest_picks_the_first_held_episode_and_uses_the_existing_resume_path(
    session_factory, health
) -> None:
    first = await _blocked_episode(session_factory, health)
    await _blocked_episode(session_factory, health)
    await _half_open(health)
    client = FakeResume()
    argv = _resume_args(extra=["--oldest", "1"])
    code, out = await _run(session_factory, argv, client=client)
    assert code == 0, out
    assert client.calls == [first]  # 新しい workflow ではなく既存の再開 API だけを呼ぶ
    assert [s["episode_id"] for s in out["started"]] == [first]
    assert await health.open_hold_count() == 1  # first の保留だけ解放した
    assert (await health.snapshot(FAL)).state is BreakerState.HALF_OPEN  # breaker は変えない


async def test_resume_dry_run_changes_nothing(session_factory, health) -> None:
    ep = await _blocked_episode(session_factory, health)
    await _half_open(health)
    client = FakeResume()
    code, out = await _run(session_factory, _resume_args(ep, apply=False), client=client)
    assert code == 0 and out["dry_run"] is True and "started" not in out
    assert client.calls == [] and await health.open_hold_count() == 1


async def test_a_resumed_episode_still_running_counts_toward_the_cap(
    session_factory, health
) -> None:
    first = await _blocked_episode(session_factory, health)
    second = await _blocked_episode(session_factory, health)
    await _half_open(health)
    client = FakeResume()
    assert (await _run(session_factory, _resume_args(first), client=client))[0] == 0
    async with session_factory() as session:  # 再開した first は実行中
        row = await session.get(EpisodeRow, uuid.UUID(first))
        assert row is not None
        row.status = EpisodeStatus.IN_PROGRESS.value
        await session.commit()
    code, out = await _run(session_factory, _resume_args(second), client=client)
    assert code == 2 and "already running 1" in out["error"]


async def test_failed_resume_restores_the_hold(session_factory, health) -> None:
    ep = await _blocked_episode(session_factory, health)
    await _half_open(health)
    code, out = await _run(session_factory, _resume_args(ep), client=FakeResume(fail=True))
    assert code == 2 and "HTTP 409" in out["error"]
    assert await health.open_hold_count() == 1


async def test_only_held_blocked_episodes_can_be_resumed(session_factory, health) -> None:
    unheld = await _blocked_episode(session_factory, health, hold=False)
    await _half_open(health)
    code, out = await _run(session_factory, _resume_args(unheld), client=FakeResume())
    assert code == 2 and "no open credit hold" in out["error"]


async def test_succeeded_scenes_are_reused_after_resume_not_regenerated(
    session_factory, artifact_store, tmp_path, health
) -> None:
    """再開後の production は、成功済みシーンを再利用ゲートで再利用する（再送・再課金しない）。"""
    ep = await _blocked_episode(session_factory, health)
    await _half_open(health)
    await _record_reusable_scene_image(
        session_factory, artifact_store, episode_id=ep, scene_id="sb1", input_hash="h" * 64
    )
    runner = PaidJobRunner(
        session_factory=session_factory,
        store=artifact_store,
        workdir=WorkDirectory(tmp_path / "work", forbidden=()),
    )
    spec = PaidJobSpec(
        episode_id=ep,
        scene_id="sb1",
        provider=ProviderCall.FAL_IMAGE,
        artifact_type=ArtifactType.SCENE_IMAGE,
        input_hash="h" * 64,
        round=1,
        current_generation_profile_id="fake-image-profile-v1",
    )
    gen = FakeImageGenerator()
    outcome = await runner.submit(
        spec, gen, ImageRequest(prompt="p", width=1080, height=1920, aspect="9:16")
    )
    assert isinstance(outcome, Reused)
    assert gen.submit_calls == 0 and gen.prepare_calls == 0


async def test_api_resume_client_calls_only_the_existing_resume_endpoint() -> None:
    import httpx

    from infrastructure.production.provider_credits import ApiResumeClient

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/bad/resume"):
            return httpx.Response(409, json={"detail": "not resumable"})
        return httpx.Response(202, json={"workflow_id": "w:1", "target_stage": "production"})

    client = ApiResumeClient("http://api.test", transport=httpx.MockTransport(handler))
    started = await client.resume("ep1")
    assert started.workflow_id == "w:1" and started.target_stage == "production"
    with pytest.raises(ResumeRejected):
        await client.resume("bad")
    assert seen == [("POST", "/episodes/ep1/resume"), ("POST", "/episodes/bad/resume")]


def test_script_has_no_network_or_provider_imports() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for banned in ("httpx", "fal_queue", "fal_storage", "requests", "urllib.request"):
        assert banned not in text
