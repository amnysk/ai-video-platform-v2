"""介入 API の裏側（JP-MAS-001 / ADR-0041、HTTP の形は ``docs/operations/intervention-api.md``）。

router（``apps/api/routers/intervention.py``）はこの ``InterventionBackend`` だけを呼ぶ。ここは

- **store だけで読めるもの**（Episode の有無・判断記録・command・ChangeSet）を
  ``InterventionStore`` で直接読み、
- **担当 A / B の関数**（read model・ReviewPacket・変更案ファイル・``receive_command``）を
  ``InterventionModules`` 経由で呼ぶ。

実モジュールの import は ``load_real_modules`` の中だけ（遅延 import）。テストは
``InterventionModules`` に fake を渡し、store と SQLite は本物を使う。

API は検証 → 保存（1 transaction・commit）→ workflow の start（best-effort）→ 返す、だけを行い、
workflow の完了は待たない（INV-16）。start の失敗はログに残し、再配送 Schedule が拾う
（``docs/domain/intervention.md`` §4）。
"""

from __future__ import annotations

import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.auth import ApiPrincipal
from contracts.intervention import (
    INTERVENTION_COMMAND_WORKFLOW,
    CommandEnvelope,
    Scope,
    intervention_command_workflow_id,
)
from infrastructure.config import Settings
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from infrastructure.intervention.store import (
    Actor,
    ChangeSet,
    Command,
    DecisionRecord,
    DuplicateCommandError,
    InterventionStore,
)

logger = logging.getLogger("avp.api.intervention")


# --------------------------------------------------------------------------- 値・例外


class EpisodeNotFoundError(LookupError):
    pass


class RevisionNotFoundError(LookupError):
    """revision が無い、またはその Episode のものでない。"""


class CommandIdReusedError(Exception):
    """同じ command_id が違う内容で来た（INV-42）。``existing`` は保存済みの command。"""

    def __init__(self, existing: Command) -> None:
        super().__init__(f"command_id {existing.command_id} was already used")
        self.existing = existing


class ProposalFileError(ValueError):
    """変更案ファイルを読めない（形が違う）。"""


@dataclass(frozen=True, slots=True)
class Received:
    command: Command
    #: False = 同じ command_id・同じ内容の再送（新しい行も適用も作っていない）
    created: bool


@dataclass(frozen=True, slots=True)
class ProposalFile:
    """取り込む変更案（担当 A の ``ChangeProposalFile`` を正規化した形）。"""

    episode_id: str | None
    #: ``ProposeChangePayload`` の JSON（検証済み）
    proposals: tuple[dict[str, Any], ...]


class CommandStarter(Protocol):
    async def start(self, command_id: str) -> None: ...


class InterventionBackend(Protocol):
    async def episode_exists(self, episode_id: str) -> bool: ...

    async def load_view(self, episode_id: str) -> Any: ...

    async def compare(self, episode_id: str, revision_a: str, revision_b: str) -> Any: ...

    async def packet(self, episode_id: str) -> Any: ...

    async def packet_markdown(self, episode_id: str) -> str: ...

    async def list_decisions(self, episode_id: str) -> list[DecisionRecord]: ...

    async def receive(
        self, episode_id: str, envelopes: Sequence[CommandEnvelope], principal: ApiPrincipal
    ) -> list[Received]: ...

    async def start_command(self, command_id: str) -> bool: ...

    async def get_command(self, command_id: str) -> Command | None: ...

    async def list_commands(self, episode_id: str) -> list[Command]: ...

    async def get_change_set(self, change_set_id: str) -> ChangeSet | None: ...

    async def list_change_sets(self, episode_id: str) -> list[ChangeSet]: ...

    def parse_proposal_file(self, raw: Mapping[str, Any]) -> ProposalFile: ...


# --------------------------------------------------------------------------- A / B の入口

#: B: ``receive_command(session, episode_id, envelope, principal) -> ReceiveResult``
#: （commit は呼び出し側）
ReceiveCommand = Callable[[AsyncSession, str, CommandEnvelope, Any], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class InterventionModules:
    """担当 A / B が提供する関数の束。実物は ``load_real_modules``、テストは fake。"""

    #: B ``commands.receive_command``
    receive_command: ReceiveCommand
    #: B ``commands.Principal(actor, scopes)`` を作る
    make_principal: Callable[[Actor, frozenset[Scope]], Any]
    #: A ``read_model.EpisodeReadModel(...)`` を作る（``load`` / ``compare_revisions`` を持つ）
    read_model: Callable[[], Any]
    #: A ``review_packet.build_packet(view)``
    build_packet: Callable[[Any], Any]
    #: A ``review_packet.render_markdown(packet)``
    render_markdown: Callable[[Any], Any]
    #: A ``contracts.review_packet.ChangeProposalFile`` を読んで正規化する
    parse_proposal_file: Callable[[Mapping[str, Any]], ProposalFile]
    #: commit 後の ``InterventionCommandWorkflow`` の start
    command_starter: CommandStarter


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _received(result: Any) -> Received:
    """B の ``ReceiveResult`` を読む（``command`` と ``created`` を持つ想定）。"""
    if isinstance(result, tuple) and len(result) == 2:  # (command, created)
        command, created = result
    else:
        command, created = getattr(result, "command", None), getattr(result, "created", None)
    if not isinstance(command, Command):
        raise TypeError(f"receive_command returned {type(command).__name__}, not store.Command")
    return Received(command=command, created=bool(created))


# --------------------------------------------------------------------------- 実装


class StoreBackedInterventionBackend:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], modules: InterventionModules
    ) -> None:
        self._session_factory = session_factory
        self._m = modules

    async def episode_exists(self, episode_id: str) -> bool:
        async with self._session_factory() as session:
            return await EpisodeRepository(session).get(episode_id) is not None

    async def load_view(self, episode_id: str) -> Any:
        return await _maybe_await(self._m.read_model().load(episode_id))

    async def revisions_belong_to(self, episode_id: str, revision_ids: Sequence[str]) -> bool:
        """revision（``artifact_metadata`` の行）が全てこの Episode のものか。"""
        async with self._session_factory() as session:
            repo = ArtifactMetadataRepository(session)
            for revision_id in revision_ids:
                meta = await repo.get(revision_id)
                if meta is None or str(meta.episode_id) != str(episode_id):
                    return False
        return True

    async def compare(self, episode_id: str, revision_a: str, revision_b: str) -> Any:
        if not await self.revisions_belong_to(episode_id, (revision_a, revision_b)):
            raise RevisionNotFoundError(f"{revision_a}/{revision_b}")
        return await _maybe_await(self._m.read_model().compare_revisions(revision_a, revision_b))

    async def packet(self, episode_id: str) -> Any:
        return await _maybe_await(self._m.build_packet(await self.load_view(episode_id)))

    async def packet_markdown(self, episode_id: str) -> str:
        return str(await _maybe_await(self._m.render_markdown(await self.packet(episode_id))))

    async def list_decisions(self, episode_id: str) -> list[DecisionRecord]:
        async with self._session_factory() as session:
            return await InterventionStore(session).list_decisions(episode_id=episode_id)

    async def receive(
        self, episode_id: str, envelopes: Sequence[CommandEnvelope], principal: ApiPrincipal
    ) -> list[Received]:
        """全 envelope を**1 transaction**で受け付けて commit する（取り込みは全部か無し）。"""
        b_principal = self._m.make_principal(
            Actor(principal.actor_type, principal.actor_id), principal.scopes
        )
        async with self._session_factory() as session:
            if await EpisodeRepository(session).get(episode_id) is None:
                raise EpisodeNotFoundError(episode_id)
            results: list[Received] = []
            try:
                for envelope in envelopes:
                    raw = await self._m.receive_command(session, episode_id, envelope, b_principal)
                    results.append(_received(raw))
            except DuplicateCommandError as exc:
                await session.rollback()
                raise CommandIdReusedError(exc.existing) from None
            await session.commit()
            return results

    async def start_command(self, command_id: str) -> bool:
        """best-effort。失敗しても例外にしない（再配送 Schedule が拾う）。"""
        try:
            await self._m.command_starter.start(command_id)
        except Exception as exc:  # noqa: BLE001 - 受付は commit 済み。start の失敗で 5xx にしない
            logger.warning(
                "intervention command workflow start failed; redelivery will retry "
                "(command_id=%s, error=%s)",
                command_id,
                type(exc).__name__,
            )
            return False
        return True

    async def get_command(self, command_id: str) -> Command | None:
        async with self._session_factory() as session:
            return await InterventionStore(session).get_command(command_id)

    async def list_commands(self, episode_id: str) -> list[Command]:
        async with self._session_factory() as session:
            return await InterventionStore(session).list_commands(episode_id)

    async def get_change_set(self, change_set_id: str) -> ChangeSet | None:
        async with self._session_factory() as session:
            return await InterventionStore(session).get_change_set(change_set_id)

    async def list_change_sets(self, episode_id: str) -> list[ChangeSet]:
        async with self._session_factory() as session:
            return await InterventionStore(session).list_change_sets(episode_id)

    def parse_proposal_file(self, raw: Mapping[str, Any]) -> ProposalFile:
        return self._m.parse_proposal_file(raw)


# --------------------------------------------------------------------------- 実接続


class TemporalCommandStarter:
    """``InterventionCommandWorkflow`` を決定的 id で start し、配送済みを記録する。

    完了は待たない（INV-16）。同じ id が走っていれば Temporal が拒否する（= 既に配送済み）。
    """

    def __init__(
        self, settings: Settings, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:  # pragma: no cover - 実接続経路
        self._settings = settings
        self._session_factory = session_factory

    async def start(self, command_id: str) -> None:  # pragma: no cover - 実接続経路
        from temporalio.client import Client
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from infrastructure.logging.setup import ensure_core_log_forwarding

        ensure_core_log_forwarding()
        client = await Client.connect(
            self._settings.temporal_address, namespace=self._settings.temporal_namespace
        )
        workflow_name, task_queue = INTERVENTION_COMMAND_WORKFLOW
        # 入力の形は担当 B の InterventionCommandWorkflow と一致させる（統合時に親が確認）
        with contextlib.suppress(WorkflowAlreadyStartedError):
            await client.start_workflow(
                workflow_name,
                {"command_id": command_id},
                id=intervention_command_workflow_id(command_id),
                task_queue=task_queue,
            )
        async with self._session_factory() as session:
            await InterventionStore(session).mark_command_dispatched(command_id)
            await session.commit()


def load_real_modules(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> InterventionModules:  # pragma: no cover - 担当 A / B の統合後に親が検査する
    """担当 A / B の実モジュールを**遅延 import**で束ねる（唯一の接続点）。

    A / B のモジュールはこの branch に無いので ``importlib`` で引く
    （静的解析の対象外。統合後に親が検査）。
    """
    import importlib

    from contracts.intervention import ProposeChangePayload
    from infrastructure.storage.minio_store import MinioArtifactStore

    commands = importlib.import_module("infrastructure.intervention.commands")  # 担当 B
    read_model_mod = importlib.import_module("infrastructure.intervention.read_model")  # 担当 A
    packet_mod = importlib.import_module("infrastructure.intervention.review_packet")  # 担当 A
    change_proposal_file = importlib.import_module(
        "contracts.review_packet"
    ).ChangeProposalFile  # 担当 A
    episode_read_model = read_model_mod.EpisodeReadModel

    artifact_store = MinioArtifactStore.from_settings(settings)

    def parse(raw: Mapping[str, Any]) -> ProposalFile:
        from pydantic import ValidationError

        try:
            parsed = change_proposal_file.model_validate(raw)
        except ValidationError as exc:
            raise ProposalFileError(str(exc)) from None
        episode_id = getattr(parsed, "episode_id", None)
        proposals = tuple(
            ProposeChangePayload.model_validate(
                p.model_dump(mode="json") if hasattr(p, "model_dump") else p
            ).model_dump(mode="json", exclude_none=True)
            for p in parsed.proposals
        )
        return ProposalFile(
            episode_id=None if episode_id is None else str(episode_id), proposals=proposals
        )

    return InterventionModules(
        receive_command=commands.receive_command,
        make_principal=lambda actor, scopes: commands.Principal(actor=actor, scopes=scopes),
        read_model=lambda: episode_read_model(session_factory, artifact_store),
        build_packet=packet_mod.build_packet,
        render_markdown=packet_mod.render_markdown,
        parse_proposal_file=parse,
        command_starter=TemporalCommandStarter(settings, session_factory),
    )
