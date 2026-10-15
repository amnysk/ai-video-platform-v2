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

import asyncio
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


class RevisionTypeMismatchError(ValueError):
    """違う型の revision 同士は比較できない（read model の ``ValueError``）。"""


class CommandRejectedError(Exception):
    """B の受付が拒否した（行を作らない）。``code`` は ``RejectReason`` の値。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ModulesUnavailableError(RuntimeError):
    """担当 A / B のモジュールを組めない（import の失敗等）。store だけの route は動く。"""


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
    #: B の受付拒否の例外（``code`` 属性を持つ）。``CommandRejectedError`` に写す
    receive_error_types: tuple[type[Exception], ...] = ()


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


#: commit 後の workflow start の上限（秒）。超えたら諦めて再配送 Schedule に任せる
START_TIMEOUT_SECONDS = 5.0


class StoreBackedInterventionBackend:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        modules: InterventionModules | Callable[[], InterventionModules],
        *,
        start_timeout_seconds: float = START_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._modules = modules if isinstance(modules, InterventionModules) else None
        self._load_modules = None if isinstance(modules, InterventionModules) else modules
        self._start_timeout = start_timeout_seconds

    @property
    def _m(self) -> InterventionModules:
        """A / B のモジュール。組めなければ ``ModulesUnavailableError``（次の呼び出しで再試行）。

        store だけで読める route（判断記録・command・ChangeSet）はここを通らないので動き続ける。
        """
        if self._modules is None:
            assert self._load_modules is not None
            try:
                self._modules = self._load_modules()
            except Exception as exc:
                # 例外の文言は出さない（パス・設定値が載りうる）。型名だけ
                logger.error("intervention modules unavailable (%s)", type(exc).__name__)
                raise ModulesUnavailableError(type(exc).__name__) from exc
        return self._modules

    async def episode_exists(self, episode_id: str) -> bool:
        async with self._session_factory() as session:
            return await EpisodeRepository(session).get(episode_id) is not None

    async def load_view(self, episode_id: str) -> Any:
        view = await _maybe_await(self._m.read_model().load(episode_id))
        if view is None:  # read model は Episode が無ければ None
            raise EpisodeNotFoundError(episode_id)
        return view

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
        try:
            return await _maybe_await(
                self._m.read_model().compare_revisions(revision_a, revision_b)
            )
        except ValueError as exc:  # read model: 違う型同士・revision が無い
            raise RevisionTypeMismatchError(str(exc)) from None

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
        """全 envelope を**1 transaction**で受け付けて commit する（取り込みは全部か無し）。

        同じ command_id・同じ内容でも、保存済みの主体が違えば ``CommandIdReusedError``
        （別の主体の command を自分の再送として受け取らない）。
        """
        modules = self._m
        actor = Actor(principal.actor_type, principal.actor_id)
        b_principal = modules.make_principal(actor, principal.scopes)
        async with self._session_factory() as session:
            if await EpisodeRepository(session).get(episode_id) is None:
                raise EpisodeNotFoundError(episode_id)
            results: list[Received] = []
            try:
                for envelope in envelopes:
                    raw = await modules.receive_command(session, episode_id, envelope, b_principal)
                    received = _received(raw)
                    if not received.created and received.command.actor != actor:
                        raise DuplicateCommandError(received.command)
                    results.append(received)
            except DuplicateCommandError as exc:
                await session.rollback()
                raise CommandIdReusedError(exc.existing) from None
            except modules.receive_error_types as exc:
                await session.rollback()
                code = str(getattr(exc, "code", "invalid"))
                if code == "not_found":
                    raise EpisodeNotFoundError(episode_id) from None
                raise CommandRejectedError(code, str(exc)) from None
            await session.commit()
            return results

    async def start_command(self, command_id: str) -> bool:
        """best-effort。失敗しても例外にしない（再配送 Schedule が拾う）。"""
        try:
            starter = self._m.command_starter
            await asyncio.wait_for(starter.start(command_id), timeout=self._start_timeout)
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
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        #: 接続は1つを使い回す。client は作った event loop に結びつくので、loop が変わったら捨てる
        self._client: Any = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _lock_for(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    async def _connect(self) -> Any:
        loop = asyncio.get_running_loop()
        async with self._lock_for(loop):
            if self._client is None or self._client_loop is not loop:
                from temporalio.client import Client

                from infrastructure.logging.setup import ensure_core_log_forwarding

                ensure_core_log_forwarding()
                self._client = None
                self._client = await Client.connect(
                    self._settings.temporal_address, namespace=self._settings.temporal_namespace
                )
                self._client_loop = loop
            return self._client

    async def start(self, command_id: str) -> None:
        from temporalio.exceptions import WorkflowAlreadyStartedError

        client = await self._connect()
        workflow_name, task_queue = INTERVENTION_COMMAND_WORKFLOW
        # 入力の形は担当 B の InterventionCommandWorkflow と一致させる（統合時に親が確認）
        try:
            with contextlib.suppress(WorkflowAlreadyStartedError):
                await client.start_workflow(
                    workflow_name,
                    {"command_id": command_id},
                    id=intervention_command_workflow_id(command_id),
                    task_queue=task_queue,
                )
        except Exception:
            self._client = None  # つながりが壊れているかもしれない。次回つなぎ直す
            raise
        async with self._session_factory() as session:
            await InterventionStore(session).mark_command_dispatched(command_id)
            await session.commit()


def load_real_modules(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> InterventionModules:
    """担当 A / B の実モジュールを**遅延 import**で束ねる（唯一の接続点）。

    ここで失敗しても API 全体は落ちない: backend が初回使用時に呼び、失敗は
    ``ModulesUnavailableError``（503 ``intervention_unavailable``）になる。
    """
    from contracts.intervention import ProposeChangePayload
    from contracts.review_packet import ChangeProposalFile  # 担当 A
    from infrastructure.intervention import commands  # 担当 B
    from infrastructure.intervention.read_model import EpisodeReadModel  # 担当 A
    from infrastructure.intervention.review_packet import (  # 担当 A
        build_packet,
        render_markdown,
    )
    from infrastructure.storage.minio_store import MinioArtifactStore

    change_proposal_file = ChangeProposalFile
    episode_read_model = EpisodeReadModel

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
        build_packet=build_packet,
        render_markdown=render_markdown,
        parse_proposal_file=parse,
        command_starter=TemporalCommandStarter(settings, session_factory),
        receive_error_types=(commands.CommandReceiveError,),
    )
