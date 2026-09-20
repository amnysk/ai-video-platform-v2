"""Topic Planner の Activity 群（ADR-0025）。

Activity は入力から結果を作るだけで、round の回し方・次の工程は決めない（INV-4）。
順序は ``workers/planning/topic_workflows.TopicPlannerWorkflow`` だけが持つ。

- ``topic_find_plan``: 同じ (日, strategy, content) の plan があれば返す（INV-22）
- ``topic_gather_context``: Analytics（live → 保存済み snapshot → 無し）と Content Memory
- ``topic_generate_candidates``: LLM を1回呼び、``TopicCandidateBatch`` で検査する。
  読めない・契約違反は ``TopicCandidateContractError``（retryable）。
  **修復しない・保存しない**（INV-23）
- ``topic_select_and_save``: 決定論の重複判定・採点・選択（domain）→
  plan と全候補を1 transaction で保存

Planner のコアは content profile の中身で分岐しない。形式の説明は prompt へ渡すデータだけ。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import asdict, fields
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.converter import DataConverter

from contracts.artifacts import extract_json_object
from contracts.topic_planning import (
    CONTENT_PROFILES,
    DEFAULT_PLANNER_POLICY,
    PLANNER_VERSION,
    STRATEGY_PROFILES,
    TOPIC_FIND_PLAN,
    TOPIC_GATHER_CONTEXT,
    TOPIC_GENERATE_CANDIDATES,
    TOPIC_SELECT_AND_SAVE,
    AnalyticsMode,
    AnalyticsSummary,
    FindPlanRequest,
    FindPlanResult,
    GenerateCandidatesRequest,
    GenerateCandidatesResult,
    MemoryItem,
    PlannerPolicy,
    PlanningContext,
    SelectAndSaveRequest,
    SelectAndSaveResult,
    TopicCandidate,
    TopicCandidateBatch,
    TopicPlannerInput,
)
from domain.errors import TopicCandidateContractError
from domain.script.ports import GenerationRequest, StoryGenerator
from domain.topic_planning import (
    AnalyticsProvider,
    AnalyticsReport,
    AudienceShares,
    VideoMetrics,
    derive_feature_performance,
    report_confidence,
    select,
)
from infrastructure.db.repositories import (
    AnalyticsSnapshotRepository,
    NewTopicCandidate,
    NewTopicPlan,
    TopicPlan,
    TopicPlanRepository,
)
from prompts import TOPIC_PROMPT_VERSION, render_topic_prompt

logger = logging.getLogger(__name__)

#: 取得する Analytics の窓（日数）
ANALYTICS_WINDOWS: frozenset[int] = frozenset({7, 28, 90})

#: prompt に載せる直近の Topic の数（Content Memory の末尾）
RECENT_TOPICS_IN_PROMPT = 30
#: prompt に載せる過去の Topic タイトル1件の最大文字数。Memory は過去の LLM 出力・手入力由来の
#: 信頼しないデータなので、長い文（指示の注入）を載せない
MAX_MEMORY_TITLE_CHARS = 120


# ------------------------------------------------------------------ snapshot の直列化


def report_to_payload(report: AnalyticsReport) -> dict[str, Any]:
    """``AnalyticsReport`` → ``analytics_snapshots.payload``（JSON。窓の鍵は文字列）。"""
    return {
        "videos_by_window": {
            str(window): [asdict(v) for v in videos]
            for window, videos in sorted(report.videos_by_window.items())
        },
        "audience": asdict(report.audience),
    }


def report_from_payload(payload: dict[str, Any]) -> AnalyticsReport:
    videos = payload.get("videos_by_window", {})
    return AnalyticsReport(
        videos_by_window={
            int(window): [VideoMetrics(**v) for v in items] for window, items in videos.items()
        },
        audience=AudienceShares(**payload.get("audience", {})),
    )


def _load_report(payload: dict[str, Any]) -> AnalyticsReport | None:
    """保存済み payload を読む。読めない形（別版の項目など）なら None（best-effort）。"""
    try:
        return report_from_payload(payload)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        logger.warning("saved analytics snapshot unreadable (%s); ignoring it", type(exc).__name__)
        return None


def _totals(report: AnalyticsReport) -> dict[str, dict[str, float]]:
    totals: dict[str, dict[str, float]] = {}
    for window, videos in sorted(report.videos_by_window.items()):
        totals[f"{window}d"] = {
            "videos": float(len(videos)),
            "views": sum(v.views for v in videos),
            "estimated_minutes_watched": sum(v.estimated_minutes_watched for v in videos),
            "likes": sum(v.likes for v in videos),
            "shares": sum(v.shares for v in videos),
            "subscribers_gained": sum(v.subscribers_gained for v in videos),
        }
    return totals


def _audience(shares: AudienceShares) -> dict[str, dict[str, float]]:
    """``AudienceShares`` → ``AnalyticsSummary.audience``。

    項目名は ``AudienceShares`` のフィールドから導く（ここに列挙し直さない）。単一の比率は
    ``summary`` へ、dict の内訳はフィールド名の下へ。None・空は含めない。
    """
    summary: dict[str, float] = {}
    breakdowns: dict[str, dict[str, float]] = {}
    for f in fields(shares):
        value = getattr(shares, f.name)
        if value is None:
            continue
        if isinstance(value, dict):
            if value:
                breakdowns[f.name] = {str(k): float(v) for k, v in value.items()}
        else:
            summary[f.name] = float(value)
    return {**({"summary": summary} if summary else {}), **breakdowns}


def _roundtrip(value: object) -> None:
    """workflow が Activity の結果を復号する経路（既定 converter・型注釈で復号）と同じ往復。"""
    converter = DataConverter.default.payload_converter
    converter.from_payload(converter.to_payload(value), type(value))


def ensure_decodable(context: PlanningContext) -> PlanningContext:
    """workflow が復号できない ``PlanningContext`` を返さない（ADR-0029）。

    復号の失敗は Activity の失敗ではなく workflow task の失敗になり、Temporal は成功するまで
    無限に再試行する（Activity の例外だけを見る fallback の梯子には届かない）。Analytics は
    best-effort なので、復号できない形なら analytics を捨てて ``no_analytics`` に劣化させる。
    Content Memory 側の問題は握りつぶさない（劣化しても復号できなければ例外のまま）。
    """
    try:
        _roundtrip(context)
    except Exception as exc:  # noqa: BLE001 - 型だけ書く。値・例外文は写さない（INV-20）
        logger.warning(
            "planning context is undecodable (%s); dropping analytics (no_analytics)",
            type(exc).__name__,
        )
    else:
        return context
    degraded = PlanningContext(
        request=context.request,
        analytics=AnalyticsSummary(mode=AnalyticsMode.NO_ANALYTICS.value),
        memory=context.memory,
    )
    _roundtrip(degraded)
    return degraded


def _plan_as_memory(plan: TopicPlan) -> MemoryItem:
    return MemoryItem(
        topic=plan.topic,
        subject=plan.subject,
        entities=list(plan.entities),
        era=plan.era,
        theme=plan.theme,
        angle=plan.angle,
        day=plan.plan_date.isoformat(),
        status=plan.status.value,
    )


class TopicPlannerActivities:
    """外部依存（DB・LLM・Analytics・時計）をすべて注入する（INV-18）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        generator: StoryGenerator,
        analytics: AnalyticsProvider | None,
        analytics_provider_id: str,
        clock: Callable[[], datetime],
        timeout_seconds: int,
        policy: PlannerPolicy = DEFAULT_PLANNER_POLICY,
    ) -> None:
        self._session_factory = session_factory
        self._generator = generator
        self._analytics = analytics
        self._analytics_provider_id = analytics_provider_id
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._policy = policy

    def all_activities(self) -> Sequence[Callable[..., object]]:
        """Worker へ登録する Activity（``TOPIC_PLANNER_ACTIVITY_NAMES`` と一致する）。"""
        return [
            self.find_plan,
            self.gather_context,
            self.generate_candidates,
            self.select_and_save,
        ]

    # ------------------------------------------------------------------ find

    @activity.defn(name=TOPIC_FIND_PLAN)
    async def find_plan(self, request: FindPlanRequest) -> FindPlanResult:
        async with self._session_factory() as session:
            plan = await TopicPlanRepository(session).find(
                date.fromisoformat(request.plan_date),
                request.strategy_profile_id,
                request.content_profile_id,
            )
        if plan is None:
            return FindPlanResult()
        return FindPlanResult(
            topic_plan_id=plan.id, topic=plan.topic, analytics_mode=plan.analytics_mode.value
        )

    # ------------------------------------------------------------------ context

    @activity.defn(name=TOPIC_GATHER_CONTEXT)
    async def gather_context(self, request: TopicPlannerInput) -> PlanningContext:
        """Analytics は live → 最新 snapshot（stale）→ 無し と劣化する。

        DB の失敗は握りつぶさない。
        """
        report, mode, snapshot_id = await self._analytics_report()
        async with self._session_factory() as session:
            plans = TopicPlanRepository(session)
            memory = await plans.list_memory()
            summary = AnalyticsSummary(mode=mode.value, snapshot_id=snapshot_id)
            if report is not None:
                window = self._policy.confidence_window_days
                video_ids = [v.video_id for v in report.videos_by_window.get(window, [])]
                by_video = await plans.plans_by_video_id(video_ids)
                summary.confidence = report_confidence(report, self._policy, window)
                summary.features = derive_feature_performance(
                    report, {vid: _plan_as_memory(p) for vid, p in by_video.items()}, window
                )
                summary.totals = _totals(report)
                summary.audience = _audience(report.audience)
        return ensure_decodable(PlanningContext(request=request, analytics=summary, memory=memory))

    async def _analytics_report(
        self,
    ) -> tuple[AnalyticsReport | None, AnalyticsMode, str | None]:
        if self._analytics is not None:
            try:
                report = await self._analytics.fetch(ANALYTICS_WINDOWS)
            except Exception as exc:  # noqa: BLE001 - provider の失敗は致命的でない（fallback）
                # 例外文は写さない（URL・token が混ざりうる / INV-20）。型だけ
                logger.warning("live analytics unavailable (%s); falling back", type(exc).__name__)
            else:
                async with self._session_factory() as session:
                    snapshot = await AnalyticsSnapshotRepository(session).save(
                        self._clock().date(),
                        self._analytics_provider_id,
                        report_to_payload(report),
                    )
                    await session.commit()
                # 当日の snapshot が既にあれば save は既存を返す。id が指す payload で採点する
                loaded = _load_report(snapshot.payload)
                if loaded is not None:
                    return loaded, AnalyticsMode.NORMAL, snapshot.id
        async with self._session_factory() as session:
            latest = await AnalyticsSnapshotRepository(session).latest(self._analytics_provider_id)
        loaded = _load_report(latest.payload) if latest is not None else None
        if latest is None or loaded is None:
            return None, AnalyticsMode.NO_ANALYTICS, None
        return loaded, AnalyticsMode.STALE_ANALYTICS, latest.id

    # ------------------------------------------------------------------ generate

    @activity.defn(name=TOPIC_GENERATE_CANDIDATES)
    async def generate_candidates(
        self, request: GenerateCandidatesRequest
    ) -> GenerateCandidatesResult:
        """LLM を1回呼ぶ。自動 retry しない（round は workflow が回す）。"""
        ctx = request.context
        strategy = STRATEGY_PROFILES[ctx.request.strategy_profile_id]
        content = CONTENT_PROFILES[ctx.request.content_profile_id]
        schema = TopicCandidateBatch.model_json_schema()
        subjects = sorted({m.subject for m in ctx.memory if m.subject})
        prompt = render_topic_prompt(
            strategy_json=strategy.model_dump_json(indent=2),
            format_brief=content.format_brief,
            analytics_summary_json=json.dumps(
                asdict(ctx.analytics), ensure_ascii=False, sort_keys=True, indent=2
            ),
            memory_subjects=subjects,
            recent_topics=[
                m.topic[:MAX_MEMORY_TITLE_CHARS] for m in ctx.memory[-RECENT_TOPICS_IN_PROMPT:]
            ],
            avoid_subjects=request.avoid_subjects,
            candidate_count_min=self._policy.candidate_count_min,
            candidate_count_max=self._policy.candidate_count_max,
            # prompt に埋めるスキーマと検査するモデルは同じ1つから導出する（AGENTS.md §8）
            schema_json=json.dumps(schema, ensure_ascii=False, sort_keys=True),
        )
        result = await self._generator.generate(
            GenerationRequest(
                episode_id=f"topic-plan-{ctx.request.plan_date}-r{request.round}",
                prompt=prompt,
                output_schema=schema,
                timeout_seconds=self._timeout_seconds,
            )
        )
        batch = self._validate(result.text)
        return GenerateCandidatesResult(
            candidates=[c.model_dump(mode="json") for c in batch.candidates],
            prompt_version=TOPIC_PROMPT_VERSION,
        )

    @staticmethod
    def _validate(raw_text: str) -> TopicCandidateBatch:
        """生出力 → JSON → 契約検査。**修復はしない**（ADR-0014 / INV-23）。"""
        try:
            parsed = extract_json_object(raw_text)
        except ValueError as exc:
            raise TopicCandidateContractError(str(exc)[:500]) from None
        try:
            return TopicCandidateBatch.model_validate(parsed)
        except ValidationError as exc:
            raise TopicCandidateContractError(
                f"{exc.error_count()} contract violation(s): {str(exc)[:1000]}"
            ) from None

    # ------------------------------------------------------------------ select + save

    @activity.defn(name=TOPIC_SELECT_AND_SAVE)
    async def select_and_save(self, request: SelectAndSaveRequest) -> SelectAndSaveResult:
        """決定論の選択と保存。再実行しても同じ (日, profile) には1件（INV-22）。"""
        ctx = request.context
        planner_input = ctx.request
        plan_date = date.fromisoformat(planner_input.plan_date)
        strategy = STRATEGY_PROFILES[planner_input.strategy_profile_id]
        content = CONTENT_PROFILES[planner_input.content_profile_id]
        candidates = [TopicCandidate.model_validate(c) for c in request.candidates]

        async with self._session_factory() as session:
            plans = TopicPlanRepository(session)
            existing = await plans.find(
                plan_date, planner_input.strategy_profile_id, planner_input.content_profile_id
            )
            if existing is not None:  # commit 後に応答を失った再実行
                return SelectAndSaveResult(
                    topic_plan_id=existing.id, topic=existing.topic, reused=True
                )

            selection = select(candidates, ctx, self._policy, plan_date, strategy)
            chosen = selection.chosen
            if chosen is None:
                return SelectAndSaveResult(
                    topic_plan_id=None,
                    topic=None,
                    reused=False,
                    rejected_subjects=selection.rejected_subjects,
                )

            c = chosen.candidate
            saved = await plans.save_plan(
                NewTopicPlan(
                    plan_date=plan_date,
                    strategy_profile_id=strategy.strategy_id,
                    strategy_version=strategy.version,
                    content_profile_id=content.content_profile_id,
                    content_profile_version=content.version,
                    topic=c.topic,
                    subject=c.subject,
                    angle=c.angle.value,
                    era=c.era,
                    theme=c.theme,
                    hook=c.hook,
                    entities=list(c.entities),
                    score=chosen.score,
                    score_breakdown=dict(chosen.breakdown),
                    duplicate_score=chosen.duplicate_score,
                    duplicate_level=chosen.level,
                    analytics_mode=AnalyticsMode(ctx.analytics.mode),
                    analytics_confidence=ctx.analytics.confidence,
                    analytics_snapshot_id=ctx.analytics.snapshot_id,
                    planner_version=PLANNER_VERSION,
                    prompt_version=request.prompt_version,
                ),
                [
                    NewTopicCandidate(
                        ordinal=e.ordinal,
                        round=request.round,
                        payload=e.candidate.model_dump(mode="json"),
                        subject=e.candidate.subject,
                        angle=e.candidate.angle.value,
                        duplicate_level=e.level,
                        duplicate_score=e.duplicate_score,
                        duplicate_of=e.duplicate_of,
                        score=None if e.rejected else e.score,
                        score_breakdown=dict(e.breakdown),
                        rejected=e.rejected,
                    )
                    for e in selection.evaluations
                ],
                selected_ordinal=chosen.ordinal,
            )
            await session.commit()
        return SelectAndSaveResult(
            topic_plan_id=saved.plan.id,
            topic=saved.plan.topic,
            reused=saved.reused,
            rejected_subjects=selection.rejected_subjects,
        )
