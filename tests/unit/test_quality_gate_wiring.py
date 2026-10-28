"""品質ゲートの配線（ADR-0076 / INV-104）。SQLite（インメモリ）+ 実 repository。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

import domain.quality.deterministic as deterministic
from contracts.pipeline import UploadGateRequest
from contracts.quality import (
    QUALITY_GATE_ENFORCE_ENV,
    QUALITY_RULE_VERSION,
    QualityCheck,
    QualityGate,
    QualityReason,
    QualityVerdict,
)
from contracts.render import TechnicalQaCheck, TechnicalQaReport
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall, ReservationStatus
from domain.episode.transitions import EpisodeEvent
from domain.quality.deterministic import (
    QualityInputs,
    assess_for_auto_upload,
    assess_for_manual_upload,
    build_deterministic_report,
    description_lint_provider,
    technical_qa_provider,
)
from infrastructure.config import Settings
from infrastructure.db.models import EpisodeRow
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    ProviderReservationRepository,
)
from tests.support.render_activity import TO_ASSETS_READY
from workers.pipeline.activities import PipelineActivities

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SHA = "a" * 64
IHASH = "h" * 64


def _check(verdict: QualityVerdict, *, deterministic_: bool = True) -> QualityCheck:
    ok = verdict is QualityVerdict.PASS
    return QualityCheck(
        gate=QualityGate.VISUAL,
        verdict=verdict,
        reason_code=QualityReason.OK if ok else QualityReason.VISUAL_DEFECT,
        detail="fixed text",
        deterministic=deterministic_,
    )


def _provider(*checks: QualityCheck):
    return lambda _inputs: checks


def _acts(session_factory: Any, **kw: Any) -> PipelineActivities:
    return PipelineActivities(
        session_factory=session_factory,
        paused_env=False,
        uploads_paused_env=False,
        quality_clock=lambda: NOW,
        **kw,
    )


async def _episode(session_factory: Any, *, sha: str = SHA) -> str:
    async with session_factory() as s:
        ep = await EpisodeRepository(s).create(topic="t")
        await s.execute(
            update(EpisodeRow)
            .where(EpisodeRow.id == uuid.UUID(ep.id))
            .values(status=EpisodeStatus.RENDER_READY.value)
        )
        await ArtifactMetadataRepository(s).record(
            episode_id=ep.id,
            artifact_type=ArtifactType.FINAL_VIDEO,
            schema_version="1.0",
            bucket="artifacts",
            object_key=f"episodes/{ep.id}/final.mp4",
            sha256=sha,
            input_hash=IHASH,
        )
        await s.commit()
        return ep.id


def _on(session_factory: Any, *checks: QualityCheck) -> PipelineActivities:
    return _acts(session_factory, quality_gate_enforce=True, quality_providers=[_provider(*checks)])


async def _gate(acts: PipelineActivities, ep: str):
    return await acts.upload_gate(UploadGateRequest(ep))


# ------------------------------------------------------------ auto path (flag ON)


async def test_on_without_any_deterministic_check_is_refused(session_factory) -> None:
    ep = await _episode(session_factory)
    result = await _gate(_acts(session_factory, quality_gate_enforce=True), ep)
    assert result.allowed is False
    assert result.reason == f"quality: {QualityReason.REPORT_EMPTY_CHECKS.value}"


async def test_on_with_only_an_llm_pass_is_refused(session_factory) -> None:
    """LLM の PASS は決定論の根拠にならない（INV-102）。"""
    ep = await _episode(session_factory)
    acts = _on(session_factory, _check(QualityVerdict.PASS, deterministic_=False))
    # model identity が無いので報告自体が拒否される（どちらでも許可されない）
    assert (await _gate(acts, ep)).allowed is False


async def test_on_block_is_refused(session_factory) -> None:
    ep = await _episode(session_factory)
    result = await _gate(_on(session_factory, _check(QualityVerdict.BLOCK)), ep)
    assert result.allowed is False and result.reason == "quality: report_blocked"


async def test_on_review_is_refused_and_episode_stays_render_ready(session_factory) -> None:
    ep = await _episode(session_factory)
    result = await _gate(_on(session_factory, _check(QualityVerdict.REVIEW)), ep)
    assert result.allowed is False and result.reason == "quality: report_needs_review"
    assert result.status == EpisodeStatus.RENDER_READY.value
    async with session_factory() as s:
        episode = await EpisodeRepository(s).get(ep)
    assert episode is not None and episode.status is EpisodeStatus.RENDER_READY


@pytest.mark.parametrize("verdict", [QualityVerdict.PASS, QualityVerdict.WARN])
async def test_on_pass_and_warn_are_allowed_when_everything_else_holds(
    session_factory, verdict: QualityVerdict
) -> None:
    ep = await _episode(session_factory)
    assert (await _gate(_on(session_factory, _check(verdict)), ep)).allowed is True


async def test_on_pass_still_refused_when_reservation_is_dispatched(session_factory) -> None:
    ep = await _episode(session_factory)
    async with session_factory() as s:
        repo = ProviderReservationRepository(s)
        r = await repo.reserve(
            episode_id=ep,
            provider=ProviderCall.YOUTUBE_UPLOAD,
            idempotency_key=f"upload:{ep}",
            input_hash=IHASH,
            round=1,
        )
        await s.commit()
        await repo.mark_dispatched(r.id)
        await s.commit()
        assert r.status is ReservationStatus.RESERVED
    result = await _gate(_on(session_factory, _check(QualityVerdict.PASS)), ep)
    assert result.allowed is False and "reservation" in (result.reason or "")


async def test_on_existing_refusals_come_before_quality(session_factory) -> None:
    result = await _gate(
        _on(session_factory, _check(QualityVerdict.PASS)), "00000000-0000-0000-0000-000000000000"
    )
    assert result.reason == "episode not found"


async def test_on_a_provider_exception_is_refused_never_pass(session_factory) -> None:
    def boom(_i: QualityInputs) -> Sequence[QualityCheck]:
        raise RuntimeError("secret-looking text must not leak")

    ep = await _episode(session_factory)
    acts = _acts(session_factory, quality_gate_enforce=True, quality_providers=[boom])
    result = await _gate(acts, ep)
    assert result.allowed is False
    assert result.reason == f"quality: {QualityReason.REPORT_PRODUCTION_FAILED.value}"
    assert "secret" not in (result.reason or "")


async def test_on_an_evaluate_exception_is_refused(session_factory, monkeypatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("evaluator broke")

    monkeypatch.setattr(deterministic, "evaluate_for_upload", boom)
    ep = await _episode(session_factory)
    result = await _gate(_on(session_factory, _check(QualityVerdict.PASS)), ep)
    assert result.allowed is False
    assert result.reason == f"quality: {QualityReason.REPORT_EVALUATION_ERROR.value}"


async def test_on_a_clock_exception_is_refused(session_factory) -> None:
    def broken_clock() -> datetime:
        raise RuntimeError("no clock")

    ep = await _episode(session_factory)
    acts = PipelineActivities(
        session_factory=session_factory,
        paused_env=False,
        uploads_paused_env=False,
        quality_gate_enforce=True,
        quality_providers=[_provider(_check(QualityVerdict.PASS))],
        quality_clock=broken_clock,
    )
    result = await _gate(acts, ep)
    assert result.allowed is False and result.reason == "quality: report_evaluation_error"


async def test_on_logs_the_decision_with_contract_fields(session_factory, caplog) -> None:
    ep = await _episode(session_factory)
    with caplog.at_level(logging.INFO):
        await _gate(_on(session_factory, _check(QualityVerdict.BLOCK)), ep)
    payloads = [getattr(r, "avp_log", None) or r.__dict__ for r in caplog.records]
    text = repr(payloads)
    assert ep in text and SHA in text and QUALITY_RULE_VERSION in text
    assert "report_blocked" in text and "upload.skipped" in text


# ------------------------------------------------------------ builder / freshness


def _inputs(**kw: Any) -> QualityInputs:
    return QualityInputs(episode_id="e1", final_video_sha256=SHA, input_hash=IHASH, **kw)


def test_builder_with_no_input_aggregates_to_review_with_no_model() -> None:
    report = build_deterministic_report(_inputs(), now=NOW, rule_version=QUALITY_RULE_VERSION)
    assert report.verdict is QualityVerdict.REVIEW and report.checks == ()
    assert report.model is None and report.rule_version == QUALITY_RULE_VERSION
    assert report.final_video_sha256 == SHA and report.input_hash == IHASH


def test_builder_collects_checks_from_every_provider_in_order() -> None:
    a, b = _check(QualityVerdict.PASS), _check(QualityVerdict.WARN)
    report = build_deterministic_report(
        _inputs(), now=NOW, rule_version="r", providers=[_provider(a), _provider(b)]
    )
    assert report.checks == (a, b) and report.verdict is QualityVerdict.WARN


def test_technical_qa_and_description_providers() -> None:
    qa = TechnicalQaReport(
        passed=True, checks=(TechnicalQaCheck(check="duration", passed=True, detail="ok"),)
    )
    (check,) = technical_qa_provider(_inputs(technical_qa=qa))
    assert check.verdict is QualityVerdict.PASS and check.deterministic
    assert technical_qa_provider(_inputs()) == ()
    assert description_lint_provider(_inputs()) == ()
    (clean,) = description_lint_provider(_inputs(description="a clean description"))
    assert clean.verdict is QualityVerdict.PASS
    dirty = description_lint_provider(_inputs(description=f"id {uuid.uuid4()}"))
    assert dirty[0].verdict is QualityVerdict.BLOCK
    assert dirty[0].reason_code is QualityReason.METADATA_LINT_FAILED
    # 検査の文面に入力の文字列は写らない
    assert str(uuid.UUID(int=0)) not in dirty[0].detail


def test_llm_pass_cannot_lift_a_deterministic_block() -> None:
    block = _check(QualityVerdict.BLOCK)
    report = build_deterministic_report(
        _inputs(),
        now=NOW,
        rule_version="r",
        providers=[_provider(block, _check(QualityVerdict.PASS, deterministic_=False))],
    )
    assert report.verdict is QualityVerdict.BLOCK


def test_a_report_for_another_artifact_is_refused() -> None:
    """鮮度（INV-107）: 評価時の「今の sha256」と違えば PASS でも拒否。gate は同じ関数を通す。"""
    report = build_deterministic_report(
        _inputs(), now=NOW, rule_version="r", providers=[_provider(_check(QualityVerdict.PASS))]
    )
    from domain.quality import evaluate_for_upload

    decision = evaluate_for_upload(
        report,
        current_final_video_sha256="b" * 64,
        current_input_hash=IHASH,
        current_rule_version="r",
        now=NOW,
    )
    assert decision.reason_code is QualityReason.REPORT_FINAL_VIDEO_MISMATCH


def test_a_stale_rule_version_is_refused_by_the_assessment() -> None:
    from domain.quality import evaluate_for_upload

    report = build_deterministic_report(
        _inputs(), now=NOW, rule_version="old", providers=[_provider(_check(QualityVerdict.PASS))]
    )
    decision = evaluate_for_upload(
        report,
        current_final_video_sha256=SHA,
        current_input_hash=IHASH,
        current_rule_version=QUALITY_RULE_VERSION,
        now=NOW,
    )
    assert decision.reason_code is QualityReason.REPORT_RULE_VERSION_MISMATCH


def test_manual_assessment_allows_review_but_never_block_or_failure() -> None:
    kw = {"now": NOW, "rule_version": QUALITY_RULE_VERSION}
    review = assess_for_manual_upload(
        _inputs(), providers=[_provider(_check(QualityVerdict.REVIEW))], **kw
    )
    assert review.decision.allowed and review.review_approved
    empty = assess_for_manual_upload(_inputs(), providers=[], **kw)
    assert empty.decision.allowed and empty.review_approved
    block = assess_for_manual_upload(
        _inputs(), providers=[_provider(_check(QualityVerdict.BLOCK))], **kw
    )
    assert not block.decision.allowed and not block.review_approved

    def boom(_i: QualityInputs) -> Sequence[QualityCheck]:
        raise RuntimeError("x")

    failed = assess_for_manual_upload(_inputs(), providers=[boom], **kw)
    assert not failed.decision.allowed
    assert failed.decision.reason_code is QualityReason.REPORT_PRODUCTION_FAILED
    passed = assess_for_manual_upload(
        _inputs(), providers=[_provider(_check(QualityVerdict.PASS))], **kw
    )
    assert passed.decision.allowed and not passed.review_approved


def test_auto_assessment_never_approves_review() -> None:
    a = assess_for_auto_upload(
        _inputs(),
        now=NOW,
        rule_version=QUALITY_RULE_VERSION,
        providers=[_provider(_check(QualityVerdict.REVIEW))],
    )
    assert not a.decision.allowed and not a.review_approved


# ------------------------------------------------------------ flag OFF equivalence


async def test_off_gate_ignores_quality_even_with_block_and_broken_providers(
    session_factory,
) -> None:
    def boom(_i: QualityInputs) -> Sequence[QualityCheck]:
        raise RuntimeError("must not be called")

    ep = await _episode(session_factory)
    for kw in (
        {},
        {"quality_providers": [_provider(_check(QualityVerdict.BLOCK))]},
        {"quality_providers": [boom], "quality_gate_enforce": False},
    ):
        result = await _gate(_acts(session_factory, **kw), ep)
        assert (result.allowed, result.reason, result.status) == (True, None, "render_ready")


async def test_off_gate_matches_on_gate_for_every_pre_existing_refusal(session_factory) -> None:
    """既存の拒否理由は flag に依らず同一（ON は既存の後に品質を足すだけ）。"""
    missing = "00000000-0000-0000-0000-000000000000"
    for acts_kw in ({}, {"quality_gate_enforce": True}):
        r = await _gate(_acts(session_factory, **acts_kw), missing)
        assert (r.allowed, r.reason) == (False, "episode not found")


def test_setting_defaults_off_and_reads_the_single_key(monkeypatch) -> None:
    monkeypatch.delenv(QUALITY_GATE_ENFORCE_ENV, raising=False)
    assert Settings(_env_file=None).quality_gate_enforce is False  # type: ignore[call-arg]
    monkeypatch.setenv(QUALITY_GATE_ENFORCE_ENV, "true")
    assert Settings(_env_file=None).quality_gate_enforce is True  # type: ignore[call-arg]
    assert QUALITY_GATE_ENFORCE_ENV == "AVP_QUALITY_GATE_ENFORCE"


# ------------------------------------------------------------ manual route


class _Starter:
    def __init__(self) -> None:
        self.started: list[str] = []

    async def start_upload_workflow(self, *, episode_id: str) -> str:
        self.started.append(episode_id)
        return f"episode-{episode_id}-upload"


@pytest_asyncio.fixture
async def manual_api(session_factory):
    from apps.api.dependencies import (
        get_quality_clock,
        get_quality_providers,
        get_session_factory,
        get_settings,
        get_workflow_starter,
    )
    from apps.api.main import create_app

    state: dict[str, Any] = {"providers": []}
    starter = _Starter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_workflow_starter] = lambda: starter
    app.dependency_overrides[get_quality_providers] = lambda: state["providers"]
    app.dependency_overrides[get_quality_clock] = lambda: lambda: NOW
    state["settings"] = Settings(_env_file=None, **{QUALITY_GATE_ENFORCE_ENV: True})  # type: ignore[arg-type]
    app.dependency_overrides[get_settings] = lambda: state["settings"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, starter, state


async def _render_ready_with_owner(session_factory) -> str:
    ep = await _episode(session_factory)
    async with session_factory() as s:
        await EpisodeRepository(s).set_workflow_id(ep, f"episode-{ep}-upload:run-1")
        await s.commit()
    return ep


async def test_manual_on_block_is_409(manual_api, session_factory) -> None:
    client, starter, state = manual_api
    state["providers"] = [_provider(_check(QualityVerdict.BLOCK))]
    ep = await _render_ready_with_owner(session_factory)
    res = await client.post(f"/episodes/{ep}/upload")
    assert res.status_code == 409 and res.json()["detail"] == "quality: report_blocked"
    assert starter.started == []


async def test_manual_on_provider_failure_is_409(manual_api, session_factory) -> None:
    client, starter, state = manual_api

    def boom(_i: QualityInputs) -> Sequence[QualityCheck]:
        raise RuntimeError("x")

    state["providers"] = [boom]
    ep = await _render_ready_with_owner(session_factory)
    res = await client.post(f"/episodes/{ep}/upload")
    assert res.status_code == 409 and starter.started == []


async def test_manual_on_review_is_allowed_and_the_approval_is_logged(
    manual_api, session_factory, caplog
) -> None:
    client, starter, state = manual_api
    state["providers"] = []  # 検査なし = REVIEW
    ep = await _render_ready_with_owner(session_factory)
    with caplog.at_level(logging.INFO):
        res = await client.post(f"/episodes/{ep}/upload")
    assert res.status_code == 202 and starter.started == [ep]
    text = repr([r.__dict__ for r in caplog.records])
    assert "'review_approved': True" in text and "manual" in text and SHA in text


async def test_manual_on_pass_is_allowed(manual_api, session_factory) -> None:
    client, starter, state = manual_api
    state["providers"] = [_provider(_check(QualityVerdict.PASS))]
    ep = await _render_ready_with_owner(session_factory)
    assert (await client.post(f"/episodes/{ep}/upload")).status_code == 202
    assert starter.started == [ep]


async def test_manual_on_without_final_video_is_409(manual_api, session_factory) -> None:
    client, starter, _state = manual_api
    async with session_factory() as s:
        episodes = EpisodeRepository(s)
        episode = await episodes.create(topic="x")
        for event in [*TO_ASSETS_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.RENDER_READY]:
            await episodes.apply_event(episode.id, event)
        await s.commit()
    res = await client.post(f"/episodes/{episode.id}/upload")
    assert res.status_code == 409 and starter.started == []
