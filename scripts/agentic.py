#!/usr/bin/env python3
"""自律改善型制作（agentic）の所有者向け CLI（ADR-0043 §6）。DB と Temporal に直接つなぐ。

  scripts/agentic.py plan --template shorts_agentic     # コンパイルの確認（DB に触れない）
  scripts/agentic.py plan --episode EP                  # Episode に pin 済みの計画
  scripts/agentic.py draft --theme "How Edo firefighters fought fires"
  scripts/agentic.py start EP                           # AgenticEpisodeWorkflow（flag 必須）
  scripts/agentic.py learn EP                           # AgenticLearningWorkflow（flag 必須）

``draft`` は Episode と UserBrief を作るだけで、生成を始めない。日次の枠・TopicPlan を使わない。
``--command-id`` を省くと uuid4 を作り stderr へ出す。応答が分からないまま切れたら**同じ
``--command-id`` で再送する**（同じ内容なら同じ Episode、別内容なら ``command_id_reused``）。
``start`` / ``learn`` は ``JPMAS2_AGENTIC_WORKFLOWS=true`` でなければ ``feature_disabled``。
公開設定・予算上限を渡す引数は無い（投稿は private のみ）。

終了コード: 0 = ok、1 = 拒否（``{"error": {"code": ...}}`` を stdout）、2 = 使い方・入力。
運用は docs/operations/agentic.md。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import ValidationError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from application.errors import OperationError  # noqa: E402
from application.ports import AgenticWorkflowStarter, EpisodeDraftPort  # noqa: E402
from application.use_cases.agentic_production import (  # noqa: E402
    CreateEpisodeDraft,
    StartAgenticProduction,
)
from contracts.agentic import UserBrief, WorkflowPlan  # noqa: E402
from contracts.application import (  # noqa: E402
    CreateEpisodeDraftRequest,
    OperationErrorCode,
    StartAgenticProductionRequest,
    StartedWorkflow,
)
from contracts.states import ArtifactType  # noqa: E402
from domain.workflow_templates.compiler import (  # noqa: E402
    TemplateCompileError,
    compile_agentic_template,
    operation_code_for,
)
from infrastructure.agents.episode_artifacts import read_current_artifact  # noqa: E402
from infrastructure.storage.artifact_store import ArtifactStore  # noqa: E402


class LearningStarter(Protocol):
    async def start_learning(self, episode_id: uuid.UUID) -> StartedWorkflow: ...


class Starter(AgenticWorkflowStarter, LearningStarter, Protocol): ...


@dataclass
class Deps:
    drafts: EpisodeDraftPort
    starter: Starter
    session_factory: async_sessionmaker[AsyncSession]
    store: ArtifactStore
    #: ``Settings.jpmas2_agentic_workflows``
    agentic_enabled: bool


DepsFactory = Callable[[], Awaitable[Deps]]


async def _default_deps() -> Deps:
    from infrastructure.agents.draft_port import DbEpisodeDraftPort
    from infrastructure.config import Settings
    from infrastructure.db.session import session_factory_from_settings
    from infrastructure.storage.minio_store import MinioArtifactStore
    from infrastructure.temporal.agentic_starter import TemporalAgenticStarter
    from infrastructure.temporal.connect import connect_with_retry

    settings = Settings()
    session_factory = session_factory_from_settings(settings)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    client = await connect_with_retry(settings, max_attempts=3)
    return Deps(
        drafts=DbEpisodeDraftPort(
            session_factory=session_factory, store=store, bucket=settings.minio_bucket
        ),
        starter=TemporalAgenticStarter(client),
        session_factory=session_factory,
        store=store,
        agentic_enabled=settings.jpmas2_agentic_workflows,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic.py", description="agentic 制作の所有者向け CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="コンパイル結果、または Episode の pin 済みの計画")
    group = plan.add_mutually_exclusive_group(required=True)
    group.add_argument("--template", help="template_id（コンパイルの確認。DB に触れない）")
    group.add_argument("--episode", help="Episode id（pin 済みの計画を読む）")
    plan.add_argument("--template-version", default="1")

    draft = sub.add_parser("draft", help="Episode と UserBrief を作る（生成は始めない）")
    draft.add_argument("--theme", required=True)
    draft.add_argument("--template", default="shorts_agentic")
    draft.add_argument("--template-version", default="1")
    draft.add_argument("--tone")
    draft.add_argument("--audience", dest="target_audience")
    draft.add_argument("--duration", type=int, dest="desired_duration_seconds")
    draft.add_argument("--notes")
    draft.add_argument("--must-keep", action="append", default=[])
    draft.add_argument("--command-id")

    start = sub.add_parser("start", help="AgenticEpisodeWorkflow を起動する")
    start.add_argument("episode_id")

    learn = sub.add_parser("learn", help="AgenticLearningWorkflow を起動する")
    learn.add_argument("episode_id")
    return parser


def _emit(out: TextIO, payload: Any) -> None:
    out.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")


def _error(out: TextIO, code: str, message: str, detail: Any = None) -> int:
    _emit(out, {"error": {"code": code, "message": message, "detail": detail or {}}})
    return 1


async def _plan(args: argparse.Namespace, make_deps: DepsFactory, out: TextIO) -> int:
    if args.template is not None:
        try:
            plan = compile_agentic_template(args.template, args.template_version)
        except TemplateCompileError as exc:
            return _error(out, operation_code_for(exc).value, str(exc))
        _emit(out, {"pinned": False, "plan": plan.model_dump(mode="json")})
        return 0
    deps = await make_deps()
    try:
        episode_id = uuid.UUID(args.episode)
    except ValueError:
        return _error(out, OperationErrorCode.INVALID_PAYLOAD.value, "episode must be a UUID")
    async with deps.session_factory() as session:
        current = await read_current_artifact(
            session, deps.store, episode_id, ArtifactType.WORKFLOW_PLAN
        )
    if current is None:
        return _error(out, "no_pinned_plan", f"episode {episode_id} has no pinned plan")
    assert isinstance(current[1], WorkflowPlan)
    _emit(
        out, {"pinned": True, "revision_id": current[0], "plan": current[1].model_dump(mode="json")}
    )
    return 0


async def _draft(args: argparse.Namespace, make_deps: DepsFactory, out: TextIO, err: TextIO) -> int:
    fields: dict[str, Any] = {
        "theme": args.theme,
        "template_id": args.template,
        "template_version": args.template_version,
        "must_keep": tuple(args.must_keep),
    }
    for name in ("tone", "target_audience", "desired_duration_seconds", "notes"):
        value = getattr(args, name)
        if value is not None:
            fields[name] = value
    command_id = args.command_id or str(uuid.uuid4())
    err.write(f"command_id={command_id}\n")
    try:
        request = CreateEpisodeDraftRequest(command_id=command_id, brief=UserBrief(**fields))
    except ValidationError as exc:
        err.write(f"invalid input: {exc.error_count()} error(s)\n{exc}\n")
        return 2
    deps = await make_deps()
    result = await CreateEpisodeDraft(deps.drafts).execute(request)
    _emit(out, result.model_dump(mode="json"))
    return 0


def _episode_uuid(value: str, err: TextIO) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        err.write("episode_id must be a UUID\n")
        return None


async def _start(args: argparse.Namespace, make_deps: DepsFactory, out: TextIO, err: TextIO) -> int:
    episode_id = _episode_uuid(args.episode_id, err)
    if episode_id is None:
        return 2
    deps = await make_deps()
    result = await StartAgenticProduction(
        deps.drafts, deps.starter, enabled=deps.agentic_enabled
    ).execute(StartAgenticProductionRequest(episode_id=episode_id))
    _emit(out, result.model_dump(mode="json"))
    return 0


async def _learn(args: argparse.Namespace, make_deps: DepsFactory, out: TextIO, err: TextIO) -> int:
    episode_id = _episode_uuid(args.episode_id, err)
    if episode_id is None:
        return 2
    deps = await make_deps()
    if not deps.agentic_enabled:
        return _error(
            out,
            OperationErrorCode.FEATURE_DISABLED.value,
            "agentic workflows are disabled (JPMAS2_AGENTIC_WORKFLOWS=false)",
        )
    started = await deps.starter.start_learning(episode_id)
    _emit(out, {"episode_id": str(episode_id), **started.model_dump(mode="json")})
    return 0


async def run(argv: list[str], make_deps: DepsFactory, out: TextIO, err: TextIO) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    try:
        if args.command == "plan":
            return await _plan(args, make_deps, out)
        if args.command == "draft":
            return await _draft(args, make_deps, out, err)
        if args.command == "start":
            return await _start(args, make_deps, out, err)
        return await _learn(args, make_deps, out, err)
    except OperationError as exc:
        return _error(out, exc.code.value, exc.message, exc.detail)


def main(
    argv: list[str] | None = None,
    *,
    deps_factory: DepsFactory = _default_deps,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    return asyncio.run(
        run(
            list(sys.argv[1:] if argv is None else argv),
            deps_factory,
            out or sys.stdout,
            err or sys.stderr,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
