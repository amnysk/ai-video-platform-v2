"""ApplicationService（fake port）。理由は docs/testing/jpmas2-p2a-service-rationale.md。"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pytest

from application.errors import OperationError, OperationErrorCode
from application.ports import Caller, OperationRecord, ReceivedCommand
from application.service import ApplicationService
from contracts.agentic import UserBrief
from contracts.agentic_pipeline import PipelineKind, agentic_episode_workflow_id
from contracts.application import (
    CreateEpisodeDraftRequest,
    DraftRecord,
    ProposeStoryboardChangeRequest,
    StartedWorkflow,
    WorkerCheck,
)
from contracts.application_operations import (
    OPERATIONS,
    ApplyApprovedChangeRequest,
    ChangeImpactPreview,
    EpisodeList,
    ListEpisodesRequest,
    OperationName,
    RequestHoldRequest,
    ResumeProductionRequest,
)
from contracts.intervention import (
    ActorType,
    ApplyChangePayload,
    CommandEnvelope,
    CommandStatus,
    CommandType,
    ResumePayload,
    Scope,
)

EP = uuid.uuid4()
ALL_SCOPES = frozenset(Scope)


class FakeCommands:
    def __init__(self) -> None:
        self.received: list[CommandEnvelope] = []
        self.started: list[list[str]] = []
        self.seen: set[str] = set()

    async def receive(self, episode_id: str, envelopes: Sequence[CommandEnvelope], caller: Caller):
        out = []
        for e in envelopes:
            self.received.append(e)
            created = e.command_id not in self.seen
            self.seen.add(e.command_id)
            out.append(ReceivedCommand(e.command_id, CommandStatus.RECEIVED, created))
        return out

    async def start(self, command_ids: Sequence[str]) -> None:
        self.started.append(list(command_ids))


class FakeDrafts:
    def __init__(self) -> None:
        self.created: dict[str, uuid.UUID] = {}

    async def create_draft(self, *, command_id: str, brief: UserBrief) -> DraftRecord:
        existing = self.created.get(command_id)
        if existing is not None:
            return DraftRecord(episode_id=existing, created=False)
        self.created[command_id] = uuid.uuid4()
        return DraftRecord(episode_id=self.created[command_id], created=True)

    async def get_brief(self, episode_id: uuid.UUID) -> UserBrief | None:
        return UserBrief(theme="t", template_id="shorts_agentic", template_version="1")


class FakeStarter:
    def __init__(self, check: WorkerCheck = WorkerCheck.POLLERS_PRESENT) -> None:
        self.starts = 0
        self.check = check
        self.probe_raises = False

    async def start(self, *, episode_id: uuid.UUID, template_id: str, template_version: str):
        self.starts += 1
        return StartedWorkflow(
            workflow_id=agentic_episode_workflow_id(str(episode_id)),
            started=True,
            worker_check=self.check,
        )

    async def probe_workers(self) -> WorkerCheck:
        if self.probe_raises:
            raise RuntimeError("temporal down")
        return self.check


def _preview(**over: Any) -> ChangeImpactPreview:
    base: dict[str, Any] = {
        "episode_id": EP,
        "change_set_id": "cs-1",
        "kind": "scene.override_visual",
        "status": "proposed",
        "base_revision_id": "rev-1",
        "base_manifest_id": None,
        "proposal": {},
        "impact": None,
        "estimated_cost_usd": "0.5000",
        "cost_estimated": True,
        "control_revision": 2,
        "blockers": (),
    }
    return ChangeImpactPreview.model_validate({**base, **over})


class FakeReads:
    def __init__(self) -> None:
        self.kind = PipelineKind.LEGACY
        self.unstarted_draft = False
        self.preview = _preview()
        self.calls: list[str] = []

    async def pipeline_kind(self, episode_id: uuid.UUID) -> PipelineKind:
        return self.kind

    async def is_unstarted_agentic_draft(self, episode_id: uuid.UUID) -> bool:
        return self.unstarted_draft

    async def list_episodes(
        self, *, limit: int, before: datetime | None, **_: object
    ) -> EpisodeList:
        self.calls.append(f"list:{limit}")
        return EpisodeList(episodes=(), next_before=None)

    async def change_impact(self, episode_id: uuid.UUID, change_set_id: str):
        return self.preview

    async def workspace(self, episode_id):
        raise NotImplementedError

    async def storyboard(self, episode_id):
        raise OperationError(OperationErrorCode.EPISODE_NOT_FOUND, "no")

    async def agent_decisions(self, episode_id):
        raise RuntimeError("boom with secret-token")

    async def quality_review(self, episode_id):
        raise NotImplementedError

    async def progress(self, episode_id):
        raise NotImplementedError


class Observer:
    def __init__(self) -> None:
        self.records: list[OperationRecord] = []

    def record(self, record: OperationRecord) -> None:
        self.records.append(record)


class World:
    def __init__(self, *, enabled: bool = True) -> None:
        self.commands = FakeCommands()
        self.reads = FakeReads()
        self.drafts = FakeDrafts()
        self.starter = FakeStarter()
        self.observer = Observer()
        self.service = ApplicationService(
            commands=self.commands,
            reads=self.reads,
            drafts=self.drafts,
            starter=self.starter,
            agentic_enabled=enabled,
            observer=self.observer,
        )


def human(*scopes: Scope, correlation_id: str | None = "corr-1") -> Caller:
    return Caller(ActorType.HUMAN, "h", frozenset(scopes or ALL_SCOPES), None, correlation_id)


def agent(*scopes: Scope) -> Caller:
    return Caller(ActorType.AI_ASSISTANT, "ai", frozenset(scopes or ALL_SCOPES))


def hold_request(cid: str = "hold-0000001") -> RequestHoldRequest:
    return RequestHoldRequest(command_id=cid, reason="check")


def apply_request(cid: str = "apply-000001", cap: float = 1.0) -> ApplyApprovedChangeRequest:
    return ApplyApprovedChangeRequest(command_id=cid, approved_max_cost_usd=cap)


# ---------------------------------------------------------------------------- 契約との対応


def test_every_operation_is_a_service_method_named_after_it() -> None:
    for spec in OPERATIONS:
        method = getattr(ApplicationService, spec.name.value, None)
        assert method is not None and inspect.iscoroutinefunction(method), spec.name


def test_the_service_has_no_public_method_outside_the_catalog() -> None:
    public = {n for n, _ in inspect.getmembers(ApplicationService, inspect.isfunction)}
    public = {n for n in public if not n.startswith("_")}
    assert public == {o.value for o in OperationName}


# ---------------------------------------------------------------------------- 権限


async def test_a_missing_scope_is_rejected_before_anything_is_sent() -> None:
    w = World()
    with pytest.raises(OperationError) as err:
        await w.service.request_hold(human(Scope.READ), EP, hold_request())
    assert err.value.code is OperationErrorCode.MISSING_SCOPE
    assert w.commands.received == []


async def test_read_operations_need_read_scope() -> None:
    w = World()
    with pytest.raises(OperationError) as err:
        await w.service.list_episodes(human(Scope.PROPOSE), ListEpisodesRequest())
    assert err.value.code is OperationErrorCode.MISSING_SCOPE


async def test_start_production_is_human_only_even_with_the_scope() -> None:
    w = World()
    with pytest.raises(OperationError) as err:
        await w.service.start_agentic_production(agent(Scope.RESUME), EP)
    assert err.value.code is OperationErrorCode.FORBIDDEN
    assert err.value.detail == {"reason": "human_only"}
    assert w.starter.starts == 0


async def test_a_human_can_start_production_and_the_worker_check_is_returned() -> None:
    w = World()
    result = await w.service.start_agentic_production(human(Scope.RESUME), EP)
    assert result.started and result.worker_check is WorkerCheck.POLLERS_PRESENT
    assert result.workflow_id == agentic_episode_workflow_id(str(EP))


async def test_start_production_with_the_flag_off_is_feature_disabled() -> None:
    w = World(enabled=False)
    with pytest.raises(OperationError) as err:
        await w.service.start_agentic_production(human(), EP)
    assert err.value.code is OperationErrorCode.FEATURE_DISABLED
    assert w.starter.starts == 0


# ---------------------------------------------------------------------------- 観測


async def test_one_record_per_operation_with_ids_and_no_input_text() -> None:
    w = World()
    await w.service.request_hold(human(), EP, hold_request())
    [rec] = w.observer.records
    assert rec.operation is OperationName.REQUEST_HOLD
    assert (rec.result, rec.error_code) == ("succeeded", None)
    assert rec.episode_id == str(EP) and rec.command_id == "hold-0000001"
    assert rec.correlation_id == "corr-1" and rec.actor_type is ActorType.HUMAN
    assert rec.duration_ms >= 0
    assert "check" not in repr(rec)  # reason の文面は載せない


async def test_rejections_are_recorded_with_the_error_code_and_reraised() -> None:
    w = World()
    with pytest.raises(OperationError):
        await w.service.get_storyboard(human(), EP)
    with pytest.raises(OperationError):
        await w.service.request_hold(human(Scope.READ), EP, hold_request())
    assert [(r.result, r.error_code) for r in w.observer.records] == [
        ("rejected", "episode_not_found"),
        ("rejected", "missing_scope"),
    ]


async def test_unexpected_errors_are_recorded_as_failed_without_the_message() -> None:
    w = World()
    with pytest.raises(RuntimeError):
        await w.service.get_agent_decisions(human(), EP)
    [rec] = w.observer.records
    assert rec.result == "failed" and rec.error_code is None
    assert "secret-token" not in repr(rec)


async def test_the_workflow_id_of_a_start_is_recorded() -> None:
    w = World()
    await w.service.start_agentic_production(human(), EP)
    assert w.observer.records[0].workflow_id == agentic_episode_workflow_id(str(EP))


async def test_the_service_works_without_an_observer() -> None:
    w = World()
    service = ApplicationService(
        commands=w.commands,
        reads=w.reads,
        drafts=w.drafts,
        starter=w.starter,
        agentic_enabled=True,
    )
    assert (await service.list_episodes(human(), ListEpisodesRequest())).episodes == ()


# ---------------------------------------------------------------------------- draft

BRIEF = UserBrief(theme="t", template_id="shorts_agentic", template_version="1")


async def test_the_same_draft_command_returns_the_same_episode_with_created_false() -> None:
    w = World()
    req = CreateEpisodeDraftRequest(command_id="draft-0000001", brief=BRIEF)
    first = await w.service.create_episode_draft(human(Scope.PROPOSE), req)
    second = await w.service.create_episode_draft(human(Scope.PROPOSE), req)
    assert first.created and not second.created
    assert first.episode_id == second.episode_id
    assert [r.episode_id for r in w.observer.records] == [str(first.episode_id)] * 2


# ---------------------------------------------------------------------------- hold / resume


async def test_hold_sends_a_request_hold_command_and_starts_it_once() -> None:
    w = World()
    result = await w.service.request_hold(human(), EP, hold_request())
    [env] = w.commands.received
    assert env.type is CommandType.REQUEST_HOLD
    assert env.payload == {"reason": "check"}
    assert env.correlation_id == "corr-1"
    assert result.created and w.commands.started == [["hold-0000001"]]


async def test_a_duplicate_command_is_not_started_again() -> None:
    w = World()
    await w.service.request_hold(human(), EP, hold_request())
    again = await w.service.request_hold(human(), EP, hold_request())
    assert not again.created
    assert w.commands.started == [["hold-0000001"]]


async def test_resume_of_a_legacy_episode_passes_start_stage_and_reports_unknown_workers() -> None:
    w = World()
    result = await w.service.resume_production(
        human(),
        EP,
        ResumeProductionRequest(command_id="resume-000001", start_stage="storyboard"),
    )
    [env] = w.commands.received
    assert env.type is CommandType.RESUME
    assert ResumePayload.model_validate(env.payload).start_stage == "storyboard"
    assert result.pipeline_kind is PipelineKind.LEGACY
    assert result.worker_check is WorkerCheck.UNKNOWN


async def test_resume_of_an_agentic_episode_reports_the_worker_check() -> None:
    w = World()
    w.reads.kind = PipelineKind.AGENTIC
    w.starter.check = WorkerCheck.NO_POLLERS
    result = await w.service.resume_production(
        human(), EP, ResumeProductionRequest(command_id="resume-000001")
    )
    assert result.pipeline_kind is PipelineKind.AGENTIC
    assert result.worker_check is WorkerCheck.NO_POLLERS


async def test_a_failing_worker_probe_is_unknown_not_an_error() -> None:
    w = World()
    w.reads.kind = PipelineKind.AGENTIC
    w.starter.probe_raises = True
    result = await w.service.resume_production(
        human(), EP, ResumeProductionRequest(command_id="resume-000001")
    )
    assert result.worker_check is WorkerCheck.UNKNOWN
    assert len(w.commands.received) == 1


async def test_resume_of_an_agentic_episode_with_the_flag_off_creates_no_command() -> None:
    w = World(enabled=False)
    w.reads.kind = PipelineKind.AGENTIC
    with pytest.raises(OperationError) as err:
        await w.service.resume_production(
            human(), EP, ResumeProductionRequest(command_id="resume-000001")
        )
    assert err.value.code is OperationErrorCode.FEATURE_DISABLED
    assert w.commands.received == [] and w.commands.started == []
    assert w.observer.records[0].error_code == "feature_disabled"


async def test_resume_of_an_unstarted_agentic_draft_creates_no_command() -> None:
    """draft（brief のみ・計画未 pin）は legacy に見える。resume で走らせず開始へ誘導する。"""
    w = World()
    w.reads.unstarted_draft = True
    with pytest.raises(OperationError) as err:
        await w.service.resume_production(
            human(), EP, ResumeProductionRequest(command_id="resume-000001")
        )
    assert err.value.code is OperationErrorCode.INVALID_STATE
    assert err.value.detail == {"reason": "agentic_draft_not_started"}
    assert w.commands.received == [] and w.commands.started == []
    assert w.observer.records[0].error_code == "invalid_state"


async def test_an_ai_cannot_resume_an_agentic_episode_but_may_resume_a_legacy_one() -> None:
    """agentic の再開は固定計画を歩く有料の制作（開始と同じく人だけ / ADR-0044 §1）。"""
    w = World()
    w.reads.kind = PipelineKind.AGENTIC
    with pytest.raises(OperationError) as err:
        await w.service.resume_production(
            agent(), EP, ResumeProductionRequest(command_id="resume-000001")
        )
    assert err.value.code is OperationErrorCode.FORBIDDEN
    assert err.value.detail == {"reason": "human_only"}
    assert w.commands.received == []
    # legacy は従来どおり backend の判定（AI の有料再開は backend が拒否）に任せる
    w2 = World()
    await w2.service.resume_production(
        agent(), EP, ResumeProductionRequest(command_id="resume-000001")
    )
    assert len(w2.commands.received) == 1


async def test_resume_of_a_legacy_episode_works_with_the_flag_off() -> None:
    w = World(enabled=False)
    result = await w.service.resume_production(
        human(), EP, ResumeProductionRequest(command_id="resume-000001")
    )
    assert result.created


# ---------------------------------------------------------------------------- apply


async def test_apply_sends_apply_change_with_the_approved_cap() -> None:
    w = World()
    result = await w.service.apply_approved_change(human(), EP, "cs-1", apply_request(cap=0.75))
    [env] = w.commands.received
    payload = ApplyChangePayload.model_validate(env.payload)
    assert env.type is CommandType.APPLY_CHANGE
    assert (payload.change_set_id, payload.max_estimated_cost_usd) == ("cs-1", 0.75)
    assert result.created and w.commands.started == [["apply-000001"]]


@pytest.mark.parametrize(
    ("preview", "code"),
    [
        ({"status": "applied"}, OperationErrorCode.INVALID_STATE),
        ({"status": "rejected"}, OperationErrorCode.INVALID_STATE),
        (
            {"cost_estimated": False, "estimated_cost_usd": None},
            OperationErrorCode.COST_NOT_ESTIMATED,
        ),
        ({"estimated_cost_usd": None}, OperationErrorCode.COST_NOT_ESTIMATED),
        ({"estimated_cost_usd": "1.5000"}, OperationErrorCode.BUDGET_EXCEEDED),
    ],
)
async def test_apply_that_cannot_proceed_creates_no_command(
    preview: dict[str, Any], code: OperationErrorCode
) -> None:
    w = World()
    w.reads.preview = _preview(**preview)
    with pytest.raises(OperationError) as err:
        await w.service.apply_approved_change(human(), EP, "cs-1", apply_request(cap=1.0))
    assert err.value.code is code
    assert w.commands.received == [] and w.commands.started == []
    assert w.observer.records[0].error_code == code.value


async def test_a_cost_equal_to_the_cap_is_allowed_and_none_is_never_zero() -> None:
    w = World()
    w.reads.preview = _preview(estimated_cost_usd="1.0000")
    await w.service.apply_approved_change(human(), EP, "cs-1", apply_request(cap=1.0))
    assert len(w.commands.received) == 1
    w2 = World()
    w2.reads.preview = _preview(cost_estimated=False, estimated_cost_usd=None)
    with pytest.raises(OperationError):  # 上限 0 でも「0 円」とは見なさない
        await w2.service.apply_approved_change(human(), EP, "cs-1", apply_request(cap=0.0))


async def test_a_resend_of_the_apply_that_decided_the_change_reaches_the_backend() -> None:
    """適用済みにした同じ command の再送は ``invalid_state`` にせず backend の冪等判定に渡す。"""
    w = World()
    w.reads.preview = _preview(status="applied", decided_by_command_id="apply-000001")
    await w.service.apply_approved_change(human(), EP, "cs-1", apply_request(cap=1.0))
    [env] = w.commands.received
    assert env.command_id == "apply-000001"
    # 別の command で適用済みの変更をもう一度適用しようとするのは従来どおり拒否
    w2 = World()
    w2.reads.preview = _preview(status="applied", decided_by_command_id="apply-other")
    with pytest.raises(OperationError) as err:
        await w2.service.apply_approved_change(human(), EP, "cs-1", apply_request(cap=1.0))
    assert err.value.code is OperationErrorCode.INVALID_STATE
    assert w2.commands.received == []


async def test_apply_needs_the_apply_scope_before_reading_the_change_set() -> None:
    w = World()
    with pytest.raises(OperationError) as err:
        await w.service.apply_approved_change(human(Scope.READ), EP, "cs-1", apply_request())
    assert err.value.code is OperationErrorCode.MISSING_SCOPE


# ---------------------------------------------------------------------------- propose


async def test_propose_delegates_and_carries_the_correlation_id() -> None:
    w = World()
    request = ProposeStoryboardChangeRequest.model_validate(
        {
            "command_id": "req-00000001",
            "proposal": {
                "episode_id": str(EP),
                "base_storyboard_revision_id": "rev-1",
                "edits": [{"scene_id": "sb1", "visual_description": "castle"}],
            },
        }
    )
    result = await w.service.propose_storyboard_change(human(), EP, request)
    assert len(result.commands) == 1
    assert w.commands.received[0].correlation_id == "corr-1"
    assert w.observer.records[0].command_id == "req-00000001"
