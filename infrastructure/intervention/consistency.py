"""DB（正本）とオブジェクトストアの整合検査（設計 §14「バックアップと復元で、RDB の参照と
オブジェクトの整合性を検査する」/ 設計 §17-11）。**読み取りだけ**（書かない・直さない）。

検査すること:

1. ``artifact_metadata`` の各行: ``object_key`` の実体がストアにあり、sha256 が行と一致する。
   本体が JSON なら、中の媒体参照（``{"object_key", "sha256"}`` の組。``MediaDescriptor`` 等）も
   同じ検査
   （``check_media=False`` で省ける。完成動画は大きい）。
2. ``agent_runs`` の各行: snapshot / request / response の参照が実在し sha256 が一致する。
3. 判断記録と介入の行の参照が解決する:
   ``artifact_manifests.members`` → ``artifact_metadata``（同じ Episode）、``parent_manifest_id``、
   ``revision_links`` の両端（同じ Episode）、
   ``change_sets`` の revision・manifest・command、
   ``decision_records`` の ``result_revision_id``・``agent_run_id``・``command_id``、
   ``annotations`` の ``manifest_id``・``refers_to_annotation_id``・``agent_run_id``・
   ``command_id``、
   ``episode_controls`` の ``active_manifest_id``・``waiting_manifest_id``（同じ Episode）、
   ``agent_runs.output_revision_id``、``episode_events.command_id``。

``artifact_metadata.bucket`` は見ない（復元先のバケット名が違っても、渡されたストアで検査する）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infrastructure.db.models import (
    AgentRunRow,
    AnnotationRow,
    ArtifactManifestRow,
    ArtifactMetadataRow,
    ChangeSetRow,
    DecisionRecordRow,
    EpisodeControlRow,
    EpisodeEventRow,
    InterventionCommandRow,
    RevisionLinkRow,
)


class _Store(Protocol):
    async def sha256_of(self, key: str) -> str: ...

    async def get_json(self, key: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Problem:
    #: ``missing_object`` / ``sha256_mismatch`` / ``dangling_ref`` / ``episode_mismatch`` /
    #: ``unreadable``
    kind: str
    table: str
    row_id: str
    detail: str

    def line(self) -> str:
        return f"{self.kind}\t{self.table}\t{self.row_id}\t{self.detail}"


@dataclass(slots=True)
class Report:
    problems: list[Problem] = field(default_factory=list)
    checked_objects: int = 0
    checked_refs: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


_BATCH = 500


async def _rows(
    factory: async_sessionmaker[AsyncSession], model: Any, order: Any
) -> AsyncIterator[Any]:
    """表を順に読む（keyset ではなく offset。検査は1回限りの読み取り）。"""
    offset = 0
    while True:
        async with factory() as session:
            rows = list(
                await session.scalars(select(model).order_by(order).offset(offset).limit(_BATCH))
            )
        for row in rows:
            yield row
        if len(rows) < _BATCH:
            return
        offset += _BATCH


def _media_refs(payload: Any, path: str = "$") -> Iterator[tuple[str, str, str]]:
    """JSON の中の ``{"object_key": str, "sha256": str}`` の組（媒体本体の参照）。"""
    if isinstance(payload, dict):
        key, digest = payload.get("object_key"), payload.get("sha256")
        if isinstance(key, str) and isinstance(digest, str):
            yield path, key, digest
        for name, value in payload.items():
            yield from _media_refs(value, f"{path}.{name}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            yield from _media_refs(value, f"{path}[{index}]")


class ConsistencyChecker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: _Store,
        *,
        check_media: bool = True,
    ) -> None:
        self._factory = session_factory
        self._store = store
        self._check_media = check_media
        self._report = Report()

    async def run(self) -> Report:
        artifacts = await self._check_artifacts()
        manifests = await self._manifests(artifacts)
        commands = await self._ids(InterventionCommandRow)
        agent_runs = await self._check_agent_runs(artifacts)
        await self._check_links(artifacts)
        await self._check_change_sets(artifacts, manifests, commands)
        await self._check_decisions(artifacts, agent_runs, commands)
        await self._check_annotations(manifests, agent_runs, commands)
        await self._check_controls(manifests)
        await self._check_events(commands)
        return self._report

    # ------------------------------------------------------------------ 共通

    def _problem(self, kind: str, table: str, row_id: Any, detail: str) -> None:
        self._report.problems.append(Problem(kind, table, str(row_id), detail))

    async def _object(self, table: str, row_id: Any, key: str, expected: str) -> bool:
        self._report.checked_objects += 1
        try:
            actual = await self._store.sha256_of(key)
        except KeyError:
            self._problem("missing_object", table, row_id, key)
            return False
        if actual != expected:
            self._problem("sha256_mismatch", table, row_id, f"{key} store={actual} db={expected}")
            return False
        return True

    def _ref(
        self,
        table: str,
        row_id: Any,
        column: str,
        value: Any,
        targets: dict[str, str | None],
        *,
        episode_id: Any = None,
    ) -> None:
        """``value`` が ``targets``（id → episode_id）に在るか。

        ``episode_id`` を渡すと、参照先が同じ Episode かも見る。
        """
        if value is None:
            return
        self._report.checked_refs += 1
        key = str(value)
        if key not in targets:
            self._problem("dangling_ref", table, row_id, f"{column}={key}")
            return
        owner = targets[key]
        if episode_id is not None and owner is not None and owner != str(episode_id):
            self._problem("episode_mismatch", table, row_id, f"{column}={key} episode={owner}")

    async def _ids(self, model: Any) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        async for row in _rows(self._factory, model, model.id):
            episode = getattr(row, "episode_id", None)
            out[str(row.id)] = None if episode is None else str(episode)
        return out

    # ------------------------------------------------------------------ 表ごと

    async def _check_artifacts(self) -> dict[str, str | None]:
        artifacts: dict[str, str | None] = {}
        async for row in _rows(self._factory, ArtifactMetadataRow, ArtifactMetadataRow.id):
            artifacts[str(row.id)] = str(row.episode_id)
            ok = await self._object("artifact_metadata", row.id, row.object_key, row.sha256)
            if not (ok and self._check_media and row.object_key.endswith(".json")):
                continue
            try:
                payload = await self._store.get_json(row.object_key)
            except Exception as exc:  # 読めない JSON は sha が合っていても報告する
                self._problem("unreadable", "artifact_metadata", row.id, type(exc).__name__)
                continue
            for _path, key, digest in _media_refs(payload):
                if key != row.object_key:
                    await self._object("artifact_metadata", row.id, key, digest)
        return artifacts

    async def _manifests(self, artifacts: dict[str, str | None]) -> dict[str, str | None]:
        manifests = await self._ids(ArtifactManifestRow)
        async for row in _rows(self._factory, ArtifactManifestRow, ArtifactManifestRow.id):
            for slot, revision in dict(row.members).items():
                self._ref(
                    "artifact_manifests",
                    row.id,
                    f"members[{slot}]",
                    revision,
                    artifacts,
                    episode_id=row.episode_id,
                )
            self._ref(
                "artifact_manifests",
                row.id,
                "parent_manifest_id",
                row.parent_manifest_id,
                manifests,
                episode_id=row.episode_id,
            )
        return manifests

    async def _check_agent_runs(self, artifacts: dict[str, str | None]) -> dict[str, str | None]:
        runs: dict[str, str | None] = {}
        async for row in _rows(self._factory, AgentRunRow, AgentRunRow.id):
            runs[str(row.id)] = None if row.episode_id is None else str(row.episode_id)
            await self._object("agent_runs", row.id, row.snapshot_object_key, row.snapshot_sha256)
            for key, digest in (
                (row.request_object_key, row.request_sha256),
                (row.response_object_key, row.response_sha256),
            ):
                if key is not None:
                    if digest is None:
                        self._problem("dangling_ref", "agent_runs", row.id, f"{key} has no sha256")
                    else:
                        await self._object("agent_runs", row.id, key, digest)
            self._ref(
                "agent_runs",
                row.id,
                "output_revision_id",
                row.output_revision_id,
                artifacts,
                episode_id=row.episode_id,
            )
        return runs

    async def _check_links(self, artifacts: dict[str, str | None]) -> None:
        async for row in _rows(self._factory, RevisionLinkRow, RevisionLinkRow.id):
            for column in ("upstream_revision_id", "downstream_revision_id"):
                self._ref(
                    "revision_links",
                    row.id,
                    column,
                    getattr(row, column),
                    artifacts,
                    episode_id=row.episode_id,
                )

    async def _check_change_sets(
        self,
        artifacts: dict[str, str | None],
        manifests: dict[str, str | None],
        commands: dict[str, str | None],
    ) -> None:
        async for row in _rows(self._factory, ChangeSetRow, ChangeSetRow.id):
            ep = row.episode_id
            self._ref(
                "change_sets",
                row.id,
                "base_revision_id",
                row.base_revision_id,
                artifacts,
                episode_id=ep,
            )
            self._ref(
                "change_sets",
                row.id,
                "applied_revision_id",
                row.applied_revision_id,
                artifacts,
                episode_id=ep,
            )
            self._ref(
                "change_sets",
                row.id,
                "base_manifest_id",
                row.base_manifest_id,
                manifests,
                episode_id=ep,
            )
            self._ref(
                "change_sets",
                row.id,
                "applied_manifest_id",
                row.applied_manifest_id,
                manifests,
                episode_id=ep,
            )
            self._ref(
                "change_sets",
                row.id,
                "proposed_by_command_id",
                row.proposed_by_command_id,
                commands,
                episode_id=ep,
            )
            self._ref(
                "change_sets",
                row.id,
                "decided_by_command_id",
                row.decided_by_command_id,
                commands,
                episode_id=ep,
            )

    async def _check_decisions(
        self,
        artifacts: dict[str, str | None],
        runs: dict[str, str | None],
        commands: dict[str, str | None],
    ) -> None:
        async for row in _rows(self._factory, DecisionRecordRow, DecisionRecordRow.id):
            ep = row.episode_id
            self._ref(
                "decision_records",
                row.id,
                "result_revision_id",
                row.result_revision_id,
                artifacts,
                episode_id=ep,
            )
            self._ref(
                "decision_records", row.id, "agent_run_id", row.agent_run_id, runs, episode_id=ep
            )
            self._ref(
                "decision_records", row.id, "command_id", row.command_id, commands, episode_id=ep
            )

    async def _check_annotations(
        self,
        manifests: dict[str, str | None],
        runs: dict[str, str | None],
        commands: dict[str, str | None],
    ) -> None:
        annotations = await self._ids(AnnotationRow)
        async for row in _rows(self._factory, AnnotationRow, AnnotationRow.id):
            ep = row.episode_id
            self._ref(
                "annotations", row.id, "manifest_id", row.manifest_id, manifests, episode_id=ep
            )
            self._ref(
                "annotations",
                row.id,
                "refers_to_annotation_id",
                row.refers_to_annotation_id,
                annotations,
                episode_id=ep,
            )
            self._ref("annotations", row.id, "agent_run_id", row.agent_run_id, runs, episode_id=ep)
            self._ref("annotations", row.id, "command_id", row.command_id, commands, episode_id=ep)

    async def _check_controls(self, manifests: dict[str, str | None]) -> None:
        async for row in _rows(self._factory, EpisodeControlRow, EpisodeControlRow.episode_id):
            ep = row.episode_id
            self._ref(
                "episode_controls",
                ep,
                "active_manifest_id",
                row.active_manifest_id,
                manifests,
                episode_id=ep,
            )
            self._ref(
                "episode_controls",
                ep,
                "waiting_manifest_id",
                row.waiting_manifest_id,
                manifests,
                episode_id=ep,
            )

    async def _check_events(self, commands: dict[str, str | None]) -> None:
        async for row in _rows(self._factory, EpisodeEventRow, EpisodeEventRow.id):
            self._ref(
                "episode_events",
                f"{row.episode_id}:{row.seq}",
                "command_id",
                row.command_id,
                commands,
                episode_id=row.episode_id,
            )


async def check_consistency(
    session_factory: async_sessionmaker[AsyncSession], store: _Store, *, check_media: bool = True
) -> Report:
    return await ConsistencyChecker(session_factory, store, check_media=check_media).run()


__all__ = ["ConsistencyChecker", "Problem", "Report", "check_consistency"]
