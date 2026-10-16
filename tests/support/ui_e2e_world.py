"""運用者 UI のブラウザ E2E 用の「世界」（理由は docs/testing/jpmas2-p2b-rationale.md §7）。

本物の FastAPI app と本物の ``ApplicationService``（権限・人限定の検査は本物）に、
メモリ上の port を差す。
有料 API・DB・Temporal・MinIO には触れない。

- ``World`` が episode・変更案・control revision・受け付けた command を持つ。
- ``WorldCommandPort`` は backend の冪等・競合の規則を再現する（同じ command_id・同じ内容は
  ``created=False``、違う内容は 409、``expected_control_revision`` の不一致は 409）。
- ``RequestLog`` は ASGI の外側で全リクエストを記録する。ボタンを隠しただけではなく、
  **サーバーが受けて拒否した**ことを確かめるため。
- ``/__e2e/*`` はテスト専用の管理口（この module が app に足す。本番 app には存在しない）。
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from application.errors import OperationError, OperationErrorCode
from application.ports import Caller, ReceivedCommand
from application.service import ApplicationService
from apps.api.auth import token_sha256
from contracts.agentic_pipeline import PipelineKind
from contracts.application import (
    DraftRecord,
    StartedWorkflow,
    WorkerCheck,
)
from contracts.application_operations import (
    ChangeImpactPreview,
    ChangeSetSummary,
    EpisodeList,
    EpisodeSummary,
    EpisodeWorkspace,
    GetAgentDecisionsResult,
    GetQualityReviewResult,
    GetStoryboardResult,
    JobProgress,
    StoryboardView,
    WorkflowProgress,
)
from contracts.artifacts import StoryboardScene, StoryboardVisualKind
from contracts.intervention import CommandEnvelope, CommandStatus, Scope
from contracts.states import EpisodeStatus
from infrastructure.intervention.read_model import (
    CommandView,
    CostView,
    EpisodeView,
    ManifestView,
    OpenReservations,
)

TOKENS = {
    "owner": "e2e-owner-token-0001",
    "reader": "e2e-reader-token-0002",
    "assistant": "e2e-assistant-token-0003",
}

BASE_TIME = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SAME_TIME = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
PAGING_COUNT = 125

EP_DRAFT = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
EP_RUNNING = uuid.UUID("00000000-0000-4000-8000-0000000000a2")
EP_FAILED = uuid.UUID("00000000-0000-4000-8000-0000000000f3")
EP_AWAITING = uuid.UUID("00000000-0000-4000-8000-0000000000c4")
CS_OK = "cs-with-estimate"
CS_NO_ESTIMATE = "cs-no-estimate"


def principals_json() -> str:
    return json.dumps(
        {
            token_sha256(TOKENS["owner"]): {
                "actor_type": "human",
                "actor_id": "owner",
                "scopes": [s.value for s in Scope],
            },
            token_sha256(TOKENS["reader"]): {
                "actor_type": "human",
                "actor_id": "reader",
                "scopes": ["read"],
            },
            token_sha256(TOKENS["assistant"]): {
                "actor_type": "ai_assistant",
                "actor_id": "assistant",
                "scopes": ["read", "comment", "propose", "hold"],
            },
        }
    )


def _scene(n: int, text: str) -> StoryboardScene:
    return StoryboardScene(
        scene_id=f"sb{n}",
        order=n,
        script_scene_id=f"s{n}",
        start_ms=(n - 1) * 5000,
        duration_ms=5000,
        visual_kind=StoryboardVisualKind.BROLL,
        visual_description=text,
    )


@dataclass
class EpisodeState:
    summary: EpisodeSummary
    control_revision: int = 1
    change_sets: dict[str, ChangeImpactPreview] = field(default_factory=dict)
    commands: list[CommandView] = field(default_factory=list)
    started: bool = False
    failure: dict[str, Any] | None = None
    jobs: list[JobProgress] = field(default_factory=list)


class World:
    def __init__(self) -> None:
        self.episodes: dict[uuid.UUID, EpisodeState] = {}
        self.received: dict[str, tuple[str, str]] = {}  # command_id -> (episode, payload digest)
        self.accepted: list[dict[str, Any]] = []
        self.reset()

    # ------------------------------------------------------------------ 初期状態

    def reset(self) -> None:
        self.episodes = {}
        self.received = {}
        self.accepted = []

        def add(ep_id: uuid.UUID, status: str, topic: str | None, kind: PipelineKind, when: Any):
            summary = EpisodeSummary(
                episode_id=ep_id,
                status=status,
                topic=topic,
                pipeline_kind=kind,
                created_at=when,
                updated_at=when,
            )
            self.episodes[ep_id] = EpisodeState(summary=summary)
            return self.episodes[ep_id]

        add(EP_DRAFT, EpisodeStatus.PLANNED.value, None, PipelineKind.AGENTIC, BASE_TIME)
        running = add(
            EP_RUNNING,
            EpisodeStatus.IN_PROGRESS.value,
            "縄文の火おこし",
            PipelineKind.AGENTIC,
            BASE_TIME + timedelta(minutes=1),
        )
        running.started = True
        running.jobs = [
            JobProgress(
                job_id="job-1",
                type="write_script",
                status="succeeded",
                attempts=1,
                max_attempts=3,
                scene_id=None,
                updated_at=BASE_TIME,
            ),
            JobProgress(
                job_id="job-2",
                type="produce_scene_image",
                status="running",
                attempts=1,
                max_attempts=3,
                scene_id="sb1",
                updated_at=BASE_TIME,
            ),
        ]
        failed = add(
            EP_FAILED,
            EpisodeStatus.FAILED.value,
            "失敗した回",
            PipelineKind.AGENTIC,
            BASE_TIME + timedelta(minutes=2),
        )
        failed.started = True
        failed.failure = {
            "code": "provider_rejected",
            "message": "provider がプロンプトを拒否",
            "source": "job",
            "source_id": "job-9",
            "at": BASE_TIME,
        }
        failed.jobs = [
            JobProgress(
                job_id="job-9",
                type="produce_scene_image",
                status="terminal_failed",
                attempts=3,
                max_attempts=3,
                scene_id="sb1",
                updated_at=BASE_TIME,
            )
        ]
        awaiting = add(
            EP_AWAITING,
            EpisodeStatus.READY_FOR_REVIEW.value,
            "承認待ちの回",
            PipelineKind.AGENTIC,
            BASE_TIME + timedelta(minutes=3),
        )
        awaiting.started = True
        awaiting.control_revision = 4
        for cs_id, estimate in ((CS_OK, "1.2500"), (CS_NO_ESTIMATE, None)):
            awaiting.change_sets[cs_id] = ChangeImpactPreview(
                episode_id=EP_AWAITING,
                change_set_id=cs_id,
                kind="scene.override_visual",
                status="proposed",
                base_revision_id="rev-sb-1",
                base_manifest_id="manifest-1",
                proposal={"edits": [{"scene_id": "sb1", "visual_description": "新しい描写"}]},
                impact={"regenerate": ["scene_image:sb1"]},
                estimated_cost_usd=estimate,
                cost_estimated=estimate is not None,
                control_revision=awaiting.control_revision,
                blockers=() if estimate is not None else ("cost_not_estimated",),
            )
        for i in range(PAGING_COUNT):
            add(
                uuid.UUID(f"10000000-0000-4000-8000-{i:012x}"),
                EpisodeStatus.PLANNED.value if i % 2 == 0 else EpisodeStatus.COMPLETED.value,
                f"paging-{i:03d}",
                PipelineKind.LEGACY,
                SAME_TIME,
            )

    def bump_revision(self, ep_id: uuid.UUID) -> int:
        st = self.episodes[ep_id]
        st.control_revision += 1
        for cs in list(st.change_sets):
            st.change_sets[cs] = st.change_sets[cs].model_copy(
                update={"control_revision": st.control_revision}
            )
        return st.control_revision

    def _get(self, episode_id: uuid.UUID) -> EpisodeState:
        st = self.episodes.get(episode_id)
        if st is None:
            raise OperationError(OperationErrorCode.EPISODE_NOT_FOUND, "no such episode")
        return st


class WorldReads:
    def __init__(self, world: World) -> None:
        self._w = world

    async def pipeline_kind(self, episode_id: uuid.UUID) -> PipelineKind:
        return self._w._get(episode_id).summary.pipeline_kind  # pyright: ignore[reportPrivateUsage]

    async def is_unstarted_agentic_draft(self, episode_id: uuid.UUID) -> bool:
        st = self._w._get(episode_id)  # pyright: ignore[reportPrivateUsage]
        return st.summary.pipeline_kind is PipelineKind.AGENTIC and not st.started

    async def list_episodes(
        self,
        *,
        limit: int,
        before: datetime | None,
        before_id: uuid.UUID | None = None,
        status: EpisodeStatus | None = None,
        q: str | None = None,
    ) -> EpisodeList:
        rows = [s.summary for s in self._w.episodes.values()]
        if status is not None:
            rows = [r for r in rows if r.status == status.value]
        if q is not None:
            rows = [r for r in rows if r.topic is not None and q.lower() in r.topic.lower()]
        rows.sort(key=lambda r: (r.created_at, str(r.episode_id)), reverse=True)
        if before is not None:
            if before_id is None:
                rows = [r for r in rows if r.created_at < before]
            else:
                rows = [
                    r for r in rows if (r.created_at, str(r.episode_id)) < (before, str(before_id))
                ]
        page = rows[:limit]
        more = len(rows) > limit
        last = page[-1] if page else None
        return EpisodeList(
            episodes=tuple(page),
            next_before=last.created_at if more and last else None,
            next_before_id=last.episode_id if more and last else None,
        )

    async def workspace(self, episode_id: uuid.UUID) -> EpisodeWorkspace:
        st = self._w._get(episode_id)  # pyright: ignore[reportPrivateUsage]
        from infrastructure.intervention.read_model import ControlView

        control = ControlView(
            mode="manual",
            control_state="running" if st.started else "idle",
            control_revision=st.control_revision,
            policy_version="1",
            review_points=[],
            active_manifest_id="manifest-1" if st.change_sets else None,
            waiting_point=None,
            waiting_manifest_id=None,
            hold_requested_at=None,
            held_at=None,
            persisted=True,
        )
        view = EpisodeView(
            episode_id=str(episode_id),
            phase=st.summary.status,
            topic=st.summary.topic,
            topic_plan_id=None,
            blocked_reason=None,
            control=control,
            manifest=ManifestView(
                manifest_id=None,
                seq=None,
                parent_manifest_id=None,
                causation_command_id=None,
                created_at=None,
                persisted=False,
                diverges_from_current=False,
                members=[],
            ),
            decisions=[],
            agent_runs=[],
            annotations=[],
            stale_approvals=[],
            change_sets=[],
            commands=list(st.commands),
            reservations=OpenReservations(in_flight=[], unknown_outcome=[], undispatched=[]),
            costs=CostView(
                provider_usd="0.0000", research_usd="0.0000", measured_usd="0.0000", unmeasured=[]
            ),
            events=[],
            recorded_links=[],
            derived_links=[],
            unreadable_revisions=[],
        )
        pending = tuple(
            ChangeSetSummary(
                change_set_id=cs.change_set_id,
                kind=cs.kind,
                status=cs.status,
                base_revision_id=cs.base_revision_id,
                estimated_cost_usd=cs.estimated_cost_usd,
                cost_estimated=cs.cost_estimated,
                proposed_by_command_id="propose-cmd-0001",
                actor_type="ai_assistant",
                created_at=BASE_TIME.isoformat(),
            )
            for cs in st.change_sets.values()
        )
        return EpisodeWorkspace(
            episode=st.summary,
            workflow_id=f"agentic-episode-{episode_id}",
            control_revision=st.control_revision,
            pending_change_sets=pending,
            read_model=view.model_dump(mode="json"),
        )

    async def storyboard(self, episode_id: uuid.UUID) -> GetStoryboardResult:
        self._w._get(episode_id)  # pyright: ignore[reportPrivateUsage]
        return GetStoryboardResult(
            episode_id=episode_id,
            storyboard=StoryboardView(
                episode_id=episode_id,
                revision_id="rev-sb-1",
                manifest_id="manifest-1",
                schema_version="1",
                sha256="0" * 64,
                scenes=(_scene(1, "古い描写その一"), _scene(2, "古い描写その二")),
            ),
        )

    async def change_impact(self, episode_id: uuid.UUID, change_set_id: str) -> ChangeImpactPreview:
        st = self._w._get(episode_id)  # pyright: ignore[reportPrivateUsage]
        cs = st.change_sets.get(change_set_id)
        if cs is None:
            raise OperationError(OperationErrorCode.CHANGE_SET_NOT_FOUND, "no such change set")
        return cs

    async def agent_decisions(self, episode_id: uuid.UUID) -> GetAgentDecisionsResult:
        self._w._get(episode_id)  # pyright: ignore[reportPrivateUsage]
        return GetAgentDecisionsResult(episode_id=episode_id, decisions=(), agent_runs=())

    async def quality_review(self, episode_id: uuid.UUID) -> GetQualityReviewResult:
        self._w._get(episode_id)  # pyright: ignore[reportPrivateUsage]
        return GetQualityReviewResult(
            episode_id=episode_id, status="not_reviewed", review=None, artifact_id=None, findings=()
        )

    async def progress(self, episode_id: uuid.UUID) -> WorkflowProgress:
        from contracts.application_operations import FailureInfo

        st = self._w._get(episode_id)  # pyright: ignore[reportPrivateUsage]
        failure = None if st.failure is None else FailureInfo(**st.failure)
        return WorkflowProgress(
            episode_id=episode_id,
            episode_status=st.summary.status,
            pipeline_kind=st.summary.pipeline_kind,
            workflow_id=f"agentic-episode-{episode_id}",
            jobs=tuple(st.jobs),
            agent_runs=(),
            failure=failure,
        )


class WorldCommands:
    """backend の冪等・競合の規則（command_id の再利用 409、stale revision 409）。"""

    def __init__(self, world: World) -> None:
        self._w = world

    async def receive(
        self, episode_id: str, envelopes: Sequence[CommandEnvelope], caller: Caller
    ) -> list[ReceivedCommand]:
        st = self._w._get(uuid.UUID(episode_id))  # pyright: ignore[reportPrivateUsage]
        out: list[ReceivedCommand] = []
        for env in envelopes:
            digest = json.dumps(
                [env.type.value, env.expected_control_revision, env.payload], sort_keys=True
            )
            seen = self._w.received.get(env.command_id)
            if seen is not None:
                if seen != (episode_id, digest):
                    raise OperationError(
                        OperationErrorCode.COMMAND_ID_REUSED,
                        "command_id was already used with different content",
                    )
                out.append(ReceivedCommand(env.command_id, CommandStatus.RECEIVED, False))
                continue
            if (
                env.expected_control_revision is not None
                and env.expected_control_revision != st.control_revision
            ):
                raise OperationError(
                    OperationErrorCode.STALE_CONTROL_REVISION,
                    "the control revision changed",
                    {"current_control_revision": st.control_revision},
                )
            self._w.received[env.command_id] = (episode_id, digest)
            self._w.accepted.append(
                {"command_id": env.command_id, "type": env.type.value, "episode_id": episode_id}
            )
            payload = env.payload
            reason = payload.get("reason")
            st.commands.append(
                CommandView(
                    command_id=env.command_id,
                    type=env.type.value,
                    status="received",
                    done=False,
                    display_status="received (not applied yet)",
                    actor_type=caller.actor_type.value,
                    actor_id=caller.actor_id,
                    granted_scope=env.type.value,
                    reject_reason=None,
                    result=None,
                    received_at=datetime.now(UTC).isoformat(),
                    finished_at=None,
                    reason=reason if isinstance(reason, str) else None,
                    correlation_id=env.correlation_id,
                )
            )
            out.append(ReceivedCommand(env.command_id, CommandStatus.RECEIVED, True))
        return out

    async def start(self, command_ids: Sequence[str]) -> None:
        return None


class WorldDrafts:
    def __init__(self, world: World) -> None:
        self._w = world

    async def create_draft(self, *, command_id: str, brief: Any) -> DraftRecord:
        raise NotImplementedError("E2E does not create drafts")

    async def get_brief(self, episode_id: uuid.UUID) -> Any:
        return None


class WorldStarter:
    def __init__(self, world: World) -> None:
        self._w = world
        self.started: list[str] = []

    async def start(
        self, *, episode_id: uuid.UUID, template_id: str, template_version: str
    ) -> StartedWorkflow:
        self.started.append(str(episode_id))
        return StartedWorkflow(workflow_id=f"agentic-episode-{episode_id}", started=True)

    async def probe_workers(self) -> WorkerCheck:
        return WorkerCheck.UNKNOWN


def build_service(world: World) -> ApplicationService:
    return ApplicationService(
        commands=WorldCommands(world),
        reads=WorldReads(world),  # type: ignore[arg-type]
        drafts=WorldDrafts(world),  # type: ignore[arg-type]
        starter=WorldStarter(world),
        agentic_enabled=True,
    )


# ---------------------------------------------------------------------- request log


class RequestLog:
    """pure ASGI の外側 middleware。method・path・認証主体・command_id・応答 status を残す。"""

    def __init__(self, app: Any, world: World) -> None:
        self.app = app
        self.entries: list[dict[str, Any]] = []
        self._world = world

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http" or scope["path"].startswith("/__e2e/"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        principal = next(
            (n for n, t in TOKENS.items() if auth == f"Bearer {t}"),
            "none" if not auth else "unknown",
        )
        entry: dict[str, Any] = {
            "method": scope["method"],
            "path": scope["path"],
            "query": scope["query_string"].decode(),
            "principal": principal,
            "command_id": None,
            "status": None,
            "error": None,
            "correlation_id": headers.get("x-correlation-id"),
        }
        chunks: list[bytes] = []
        out_chunks: list[bytes] = []

        async def recv() -> MutableMapping[str, Any]:
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
            return msg

        async def snd(msg: MutableMapping[str, Any]) -> None:
            if msg["type"] == "http.response.start":
                entry["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                out_chunks.append(msg.get("body", b""))
            await send(msg)

        self.entries.append(entry)
        try:
            await self.app(scope, recv, snd)
        finally:
            raw = b"".join(chunks)
            if raw:
                try:
                    body = json.loads(raw)
                    entry["command_id"] = body.get("command_id")
                    entry["body_keys"] = sorted(body)
                except (ValueError, AttributeError):
                    pass
            if entry["status"] and entry["status"] >= 400 and entry["path"].startswith("/v1"):
                with contextlib.suppress(ValueError, KeyError, TypeError):
                    entry["error"] = json.loads(b"".join(out_chunks))["detail"]["error"]
