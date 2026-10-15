"""Agent への入力 document の組み立て（ADR-0043 §6 Agent stage）。

読むのは**正本だけ**: PostgreSQL（``artifact_metadata`` / ``decision_records`` /
``analytics_snapshots`` / ``episodes``）と ArtifactStore。OpenSearch（検索 index）は読まない。

document 名の正本は ``domain.agents.runtime.AGENT_INPUT_NAMES``。handler は自分の名前の
document だけを読む。この module も handler の名前に載っている document だけを組み立てる
（rule_review は reviewer を走らせるので、quality 以外では組み立てない）。

欠けている入力の表現（全 document で共通。handler の読み方に合わせる）:

- 欠けている document は**空の dict ``{}``**（handler は ``not documents[name]`` で見分ける）。
  0 や空文字で埋めない（欠測を値にしない）。理由の code は ``AssembledInputs.missing_reasons``
- ``user_brief`` と ``strategy_brief`` だけは欠けると stage が成立しないので、呼び出し側
  （Activity）が ``blocked / input_missing`` で止める（``required_inputs``）

測定値（``measurements``）は ``PublicMetricsPort`` から来る。本番の実装は収集をしない
（48h / 7d / 28d の collector は P1 の範囲外）ので ``{"public": false, "windows": [],
"reason": "collector_not_implemented"}`` を返す。隔離 E2E は mock を注入する。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.agentic import Lesson
from contracts.agents import AgentHandlerRef
from contracts.artifact_registry import UnsupportedArtifactSchemaError, parse_artifact
from contracts.states import ArtifactType
from domain.agents.runtime import AGENT_INPUT_NAMES
from infrastructure.analytics.youtube_analytics import PROVIDER_ID as ANALYTICS_PROVIDER_ID
from infrastructure.db.models import ArtifactMetadataRow, EpisodeRow
from infrastructure.db.repositories import (
    AnalyticsSnapshotRepository,
    ArtifactMetadata,
    ArtifactMetadataRepository,
)
from infrastructure.intervention.reviewer import run_reviewer
from infrastructure.intervention.store import InterventionStore
from infrastructure.storage.artifact_store import ArtifactStore

#: Strategy の入力にする他 Episode の Lesson / 題材の最大件数
MAX_LESSONS = 10
MAX_RECENT_TITLES = 30
#: producer が採用した企画の判断（``decision_records.decision_type``）
CHOOSE_TOPIC = "choose_topic"
COLLECTOR_NOT_IMPLEMENTED = "collector_not_implemented"


def missing_document(reason: str) -> dict[str, Any]:
    """欠測は空 dict。``reason`` は呼び出し側（builder）が ``missing_reasons`` に残す。"""
    del reason
    return {}


def is_missing(document: Mapping[str, Any]) -> bool:
    return not document


class PublicMetricsPort(Protocol):
    """公開後の測定（48h / 7d / 28d）。収集の実装は P1 の範囲外。"""

    async def measurements(self, *, episode_id: str, video_id: str | None) -> dict[str, Any]: ...


class NoPublicMetrics:
    """本番の既定。収集しないので「測れていない」を明示して返す（0 で埋めない）。"""

    async def measurements(self, *, episode_id: str, video_id: str | None) -> dict[str, Any]:
        return {"public": False, "windows": [], "reason": COLLECTOR_NOT_IMPLEMENTED}


@dataclass(frozen=True, slots=True)
class AssembledInputs:
    documents: dict[str, dict[str, Any]]
    #: document 名 -> 読んだ ``artifact_metadata`` の revision id（revision_links の元）
    input_revisions: dict[str, str]
    #: 実行記録に残す補足の参照（decision id・manifest id・analytics snapshot id）
    extra_refs: dict[str, str] = field(default_factory=dict)
    #: 欠けた document 名 -> 理由の code
    missing_reasons: dict[str, str] = field(default_factory=dict)

    def missing(self) -> list[str]:
        return sorted(n for n, d in self.documents.items() if is_missing(d))


#: 欠けると stage が成立しない入力
REQUIRED_INPUTS: dict[AgentHandlerRef, tuple[str, ...]] = {
    AgentHandlerRef.STRATEGY_AGENT_V1: ("user_brief",),
    AgentHandlerRef.CREATIVE_DIRECTOR_V1: ("user_brief", "strategy_brief", "producer_decision"),
    AgentHandlerRef.QUALITY_REVIEWER_V1: (),
    AgentHandlerRef.LEARNING_AGENT_V1: (),
}


def required_inputs(handler_ref: AgentHandlerRef) -> tuple[str, ...]:
    return REQUIRED_INPUTS[handler_ref]


def _condensed_final_video(document: Mapping[str, Any]) -> dict[str, Any]:
    """完成動画の document。timeline・字幕 cue は大きいので、reviewer に要る項目だけ渡す。"""
    return {
        "duration_ms": document.get("total_duration_ms"),
        "media": document.get("media"),
        "measured": document.get("measured"),
        "technical_qa": document.get("technical_qa"),
        "render_profile": document.get("render_profile"),
    }


class _Builder:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        episode_id: str,
        metrics: PublicMetricsPort,
        analytics_provider_id: str,
    ) -> None:
        self._sf = session_factory
        self._store = store
        self._episode_id = episode_id
        self._metrics = metrics
        self._analytics_provider_id = analytics_provider_id
        self.input_revisions: dict[str, str] = {}
        self.extra_refs: dict[str, str] = {}
        self.missing_reasons: dict[str, str] = {}
        self._receipt: dict[str, Any] | None = None

    # --- 共通 ---------------------------------------------------------------

    async def _current(self, artifact_type: ArtifactType) -> tuple[ArtifactMetadata, dict] | None:
        async with self._sf() as session:
            meta = await ArtifactMetadataRepository(session).find_current_by_type(
                self._episode_id, artifact_type
            )
        if meta is None:
            return None
        return meta, await self._store.get_json(meta.object_key)

    async def _registered(self, name: str, artifact_type: ArtifactType) -> dict[str, Any]:
        found = await self._current(artifact_type)
        if found is None:
            return self._absent(name, f"no_current_{artifact_type.value}")
        meta, raw = found
        try:
            parsed = parse_artifact(artifact_type, raw)
        except (UnsupportedArtifactSchemaError, ValidationError):
            return self._absent(name, f"unsupported_{artifact_type.value}_schema")
        self.input_revisions[name] = meta.id
        document = parsed.model_dump(mode="json")
        if artifact_type is ArtifactType.STORYBOARD:
            document["revision_id"] = meta.id
        return document

    def _absent(self, name: str, reason: str) -> dict[str, Any]:
        self.missing_reasons[name] = reason
        return {}

    async def _raw(
        self,
        name: str,
        artifact_type: ArtifactType,
        shape: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        found = await self._current(artifact_type)
        if found is None:
            return self._absent(name, f"no_current_{artifact_type.value}")
        meta, raw = found
        self.input_revisions[name] = meta.id
        document = shape(raw) if shape is not None else dict(raw)
        if artifact_type is ArtifactType.FINAL_VIDEO:
            document["revision_id"] = meta.id
        return document

    # --- document ごと ------------------------------------------------------

    async def user_brief(self) -> dict[str, Any]:
        return await self._registered("user_brief", ArtifactType.USER_BRIEF)

    async def strategy_brief(self) -> dict[str, Any]:
        return await self._registered("strategy_brief", ArtifactType.STRATEGY_BRIEF)

    async def creative_brief(self) -> dict[str, Any]:
        return await self._registered("creative_brief", ArtifactType.CREATIVE_BRIEF)

    async def script(self) -> dict[str, Any]:
        return await self._raw("script", ArtifactType.SCRIPT)

    async def storyboard(self) -> dict[str, Any]:
        return await self._registered("storyboard", ArtifactType.STORYBOARD)

    async def final_video(self) -> dict[str, Any]:
        return await self._raw("final_video", ArtifactType.FINAL_VIDEO, _condensed_final_video)

    async def upload_receipt(self) -> dict[str, Any]:
        if self._receipt is None:
            self._receipt = await self._raw("upload_receipt", ArtifactType.UPLOAD_RECEIPT)
        return self._receipt

    async def producer_decision(self) -> dict[str, Any]:
        async with self._sf() as session:
            decisions = await InterventionStore(session).list_decisions(episode_id=self._episode_id)
        chosen = [d for d in decisions if d.decision_type == CHOOSE_TOPIC]
        if not chosen:
            return self._absent("producer_decision", "no_choose_topic_decision")
        latest = chosen[-1]
        selected = latest.payload.get("selected_candidate")
        if not isinstance(selected, str) or not selected:
            return self._absent("producer_decision", "choose_topic_has_no_selection")
        self.extra_refs["producer_decision"] = latest.decision_id
        return {"adopted_candidate_id": selected}

    async def analytics(self) -> dict[str, Any]:
        async with self._sf() as session:
            snapshot = await AnalyticsSnapshotRepository(session).latest(
                self._analytics_provider_id
            )
        if snapshot is None:
            return {"measured": False, "reason": "no_analytics_snapshot"}
        self.extra_refs["analytics_snapshot"] = snapshot.id
        return {
            "measured": True,
            "snapshot_date": snapshot.snapshot_date.isoformat(),
            **(_analytics_summary(snapshot.payload)),
        }

    async def baseline(self) -> dict[str, Any]:
        """比較の基準。snapshot は Shorts と長尺を分けないので ``comparable`` は常に false
        （handler は「比較できる基準が無い」と書く。形式を混ぜた比較をしない）。"""
        summary = await self.analytics()
        description = None
        if summary.get("measured"):
            description = (
                f"channel snapshot of {summary.get('videos_with_views')} videos with views "
                "(shorts and long-form are not separated)"
            )
        return {
            "format": "unknown",
            "comparable": False,
            "description": description or "",
            "reason": summary.get("reason", "format_not_separated_in_snapshots"),
        }

    async def lessons(self) -> dict[str, Any]:
        """他 Episode の現行 Lesson（状態つき）。状態の解釈は handler（validated だけが参考）。"""
        async with self._sf() as session:
            rows = list(
                await session.scalars(
                    select(ArtifactMetadataRow)
                    .where(
                        ArtifactMetadataRow.artifact_type == ArtifactType.LESSON.value,
                        ArtifactMetadataRow.superseded_at.is_(None),
                        ArtifactMetadataRow.episode_id != uuid.UUID(self._episode_id),
                    )
                    .order_by(ArtifactMetadataRow.created_at.desc())
                    .limit(MAX_LESSONS)
                )
            )
            located = [(str(r.id), str(r.episode_id), r.object_key) for r in rows]
        items: list[dict[str, Any]] = []
        for revision_id, episode_id, object_key in located:
            try:
                lesson = parse_artifact(ArtifactType.LESSON, await self._store.get_json(object_key))
            except (UnsupportedArtifactSchemaError, ValidationError):
                continue  # 読めない Lesson は黙って使わない（読み替えない）
            assert isinstance(lesson, Lesson)
            items.append(
                {
                    "revision_id": revision_id,
                    "episode_id": episode_id,
                    "status": lesson.status.value,
                    "scope": lesson.scope,
                    "observations": list(lesson.observations),
                    "hypotheses": list(lesson.hypotheses),
                }
            )
        return {"items": items}

    async def recent_titles(self) -> dict[str, Any]:
        async with self._sf() as session:
            topics = list(
                await session.scalars(
                    select(EpisodeRow.topic)
                    .where(
                        EpisodeRow.topic.is_not(None),
                        EpisodeRow.id != uuid.UUID(self._episode_id),
                    )
                    .order_by(EpisodeRow.created_at.desc())
                    .limit(MAX_RECENT_TITLES)
                )
            )
        return {"titles": [t for t in topics if t]}

    async def rule_review(self) -> dict[str, Any]:
        """既存の決定論 reviewer（INV-39/45 の所見）。モデルの所見とは別物として渡す。"""
        result = await run_reviewer(self._sf, self._store, self._episode_id)
        self.extra_refs["rule_review_agent_run"] = result.agent_run_id
        self.extra_refs["manifest"] = result.manifest_id
        return {
            "verdict": result.verdict,
            "manifest_id": result.manifest_id,
            "findings": [f.document() for f in result.findings],
        }

    async def manifest(self) -> dict[str, Any]:
        manifest_id = self.extra_refs.get("manifest")
        if manifest_id is None:
            async with self._sf() as session:
                control = await InterventionStore(session).get_control(self._episode_id)
            manifest_id = control.active_manifest_id
        if manifest_id is None:
            return self._absent("manifest", "no_active_manifest")
        self.extra_refs["manifest"] = manifest_id
        return {"manifest_id": manifest_id}

    async def measurements(self) -> dict[str, Any]:
        receipt = await self.upload_receipt()
        video_id = None if is_missing(receipt) else receipt.get("video_id")
        return await self._metrics.measurements(
            episode_id=self._episode_id, video_id=video_id if isinstance(video_id, str) else None
        )


def _analytics_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``analytics_snapshots.payload`` の要約。窓ごとの合計と、views のある動画の本数。"""
    windows: dict[str, dict[str, float]] = {}
    with_views: set[str] = set()
    for window, videos in sorted((payload.get("videos_by_window") or {}).items()):
        videos = videos or []
        windows[f"{window}d"] = {
            "videos": float(len(videos)),
            "views": float(sum(float(v.get("views", 0) or 0) for v in videos)),
            "subscribers_gained": float(
                sum(float(v.get("subscribers_gained", 0) or 0) for v in videos)
            ),
        }
        with_views.update(
            str(v.get("video_id")) for v in videos if float(v.get("views", 0) or 0) > 0
        )
    return {
        "videos_with_views": len(with_views),
        "windows": windows,
        "audience": dict(payload.get("audience") or {}),
    }


async def assemble_agent_inputs(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    episode_id: str,
    handler_ref: AgentHandlerRef,
    metrics: PublicMetricsPort | None = None,
    analytics_provider_id: str = ANALYTICS_PROVIDER_ID,
) -> AssembledInputs:
    builder = _Builder(
        session_factory=session_factory,
        store=store,
        episode_id=episode_id,
        metrics=metrics or NoPublicMetrics(),
        analytics_provider_id=analytics_provider_id,
    )
    documents: dict[str, dict[str, Any]] = {}
    # rule_review が manifest を確定させてから manifest を読む（宣言順）
    for name in AGENT_INPUT_NAMES[handler_ref]:
        build: Callable[[], Awaitable[dict[str, Any]]] = getattr(builder, name)
        documents[name] = await build()
    return AssembledInputs(
        documents,
        dict(builder.input_revisions),
        dict(builder.extra_refs),
        dict(builder.missing_reasons),
    )


async def read_current_artifact(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    episode_id: str,
    artifact_type: str,
) -> dict[str, Any]:
    """tool ``artifact:read``。現行の成果物（登録済みの版だけ）。無ければ ``missing_document``。"""
    try:
        wanted = ArtifactType(artifact_type)
    except ValueError:
        return missing_document("unknown_artifact_type")
    builder = _Builder(
        session_factory=session_factory,
        store=store,
        episode_id=episode_id,
        metrics=NoPublicMetrics(),
        analytics_provider_id=ANALYTICS_PROVIDER_ID,
    )
    return await builder._registered(wanted.value, wanted)  # noqa: SLF001


async def read_analytics_summary(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    episode_id: str,
    analytics_provider_id: str = ANALYTICS_PROVIDER_ID,
) -> dict[str, Any]:
    """tool ``analytics:read``。既存の ``analytics_snapshots`` の要約（読み取りだけ）。"""
    builder = _Builder(
        session_factory=session_factory,
        store=store,
        episode_id=episode_id,
        metrics=NoPublicMetrics(),
        analytics_provider_id=analytics_provider_id,
    )
    return await builder.analytics()
