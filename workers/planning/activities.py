"""Script Worker の Activity 群。

Activity は「入力から出力Artifactを作って結果を返す」だけ。
**次に何をするかは決めない**（INV-4）。他のworkerをimportしない（INV-3）。

有料（外部AI）呼び出しの順序は ADR-0013 が権威:
予約をcommit → dispatchをcommit → 呼ぶ → spentをcommit → 検証 → Artifact。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from contracts.agentic import CreativeBrief
from contracts.artifact_registry import UnsupportedArtifactSchemaError, parse_artifact
from contracts.artifacts import (
    SCRIPT_ARTIFACT_SCHEMA_VERSION,
    ScriptArtifact,
    build_script_artifact,
    extract_json_object,
    parse_script_artifact,
)
from contracts.intervention import AgentRole, InterventionMode
from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType, ProviderCall
from contracts.topic_planning import ScriptLocale
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.episode.transitions import EpisodeEvent
from domain.errors import (
    PromptContractError,
    ScriptNarrationOverBudgetError,
    ScriptOutputUnparseableError,
    ScriptSchemaViolationError,
    UnreconciledReservationError,
    classify_failure,
)
from domain.job.transitions import JobEvent, episode_event_for_failure, job_event_for_failure
from domain.script.brief import ScriptBrief, creative_brief, legacy_brief, plan_brief
from domain.script.identity import idempotency_key, script_input_hash
from domain.script.ports import GenerationRequest, GenerationResult, StoryGenerator
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
    TopicPlanRepository,
)
from infrastructure.intervention.capture import (
    AgentRunRecorder,
    PromptSource,
    RunCapture,
    StoredSnapshot,
    request_document,
    reservation_invocation,
    response_document,
    tool_refs,
)
from infrastructure.intervention.records import (
    WRITE_SCRIPT,
    generation_decision_payload,
    record_generation_decision,
)
from infrastructure.intervention.store import InterventionStore
from infrastructure.storage.artifact_store import ArtifactStore
from prompts.script import (
    narration_budget_table,
    render_localized_script_prompt,
    script_prompt_template,
)

#: 生出力の置き場所。Artifact ではない（スキーマ検証を通らないため / ADR-0013）。
PROVIDER_RAW_PREFIX = "provider-raw"

#: モデルを固定しない運用のときに記録する値。空文字にしない。
DEFAULT_MODEL_LABEL = "codex-config-default"


@dataclass
class EpisodeRef:
    episode_id: str


@dataclass
class CreateJobRequest:
    episode_id: str
    max_attempts: int


@dataclass
class GenerateScriptRequest:
    episode_id: str
    job_id: str
    round: int


@dataclass
class ScriptResult:
    artifact_id: str
    bucket: str
    object_key: str
    sha256: str
    schema_version: str
    #: 生成器を呼ばずに既存Artifactを再利用したか（INV-17 の証拠）
    reused: bool


@dataclass
class RecordFailureRequest:
    episode_id: str
    job_id: str
    failure_class: str
    error_summary: str
    retry_exhausted: bool


@dataclass
class FailureOutcome:
    episode_status: str


class ScriptActivities:
    """外部依存をすべて注入する。テストは実サービスなしで同じコードパスを走らせる（INV-18）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        generator: StoryGenerator,
        bucket: str,
        generator_id: str,
        model: str,
        timeout_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._generator = generator
        self._bucket = bucket
        self._generator_id = generator_id
        self._model = model
        self._timeout_seconds = timeout_seconds
        #: 新方式（mode != legacy）の Episode の実行記録（ADR-0041）。legacy では使わない
        self._recorder = AgentRunRecorder(store)

    def all_activities(self) -> Sequence[Callable[..., object]]:
        """Workerへ登録するActivity。ここが登録漏れの唯一の防波堤。"""
        return [
            self.mark_episode_in_progress,
            self.create_script_job,
            self.generate_script,
            self.mark_script_ready,
            self.record_failure,
        ]

    # ------------------------------------------------------------------ 状態

    #: すでに開始済み／完了済みで、``WORKFLOW_STARTED`` を再適用してはならない状態。
    #: 遷移表は正しい（これらから ``workflow_started`` は出ない）。冪等性のために
    #: **事象を再適用しない**のであって、表を緩めるのではない。
    _ALREADY_STARTED = frozenset({EpisodeStatus.IN_PROGRESS, EpisodeStatus.SCRIPT_READY})

    @activity.defn(name="script_mark_episode_in_progress")
    async def mark_episode_in_progress(self, request: EpisodeRef) -> None:
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            episode = await episodes.get(request.episode_id)
            if episode is not None and episode.status in self._ALREADY_STARTED:
                return  # 再実行で二重遷移しない（INV-17）
            await episodes.apply_event(request.episode_id, EpisodeEvent.WORKFLOW_STARTED)
            await session.commit()

    @activity.defn(name="script_create_job")
    async def create_script_job(self, request: CreateJobRequest) -> str:
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            existing = [
                job
                for job in await jobs.list_for_episode(request.episode_id)
                if job.type is JobType.WRITE_SCRIPT
            ]
            if existing:
                return existing[0].id  # Activityの再実行でJobを重複生成しない
            job = await jobs.create(
                episode_id=request.episode_id,
                type=JobType.WRITE_SCRIPT,
                max_attempts=request.max_attempts,
            )
            await session.commit()
            return job.id

    @activity.defn(name="script_mark_ready")
    async def mark_script_ready(self, request: EpisodeRef) -> str:
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            current = await episodes.get(request.episode_id)
            if current is not None and current.status is EpisodeStatus.SCRIPT_READY:
                return current.status.value  # 再実行で二重遷移しない（INV-17）
            episode = await episodes.apply_event(request.episode_id, EpisodeEvent.SCRIPT_READY)
            await session.commit()
            return episode.status.value

    # ------------------------------------------------------------------ 生成

    @activity.defn(name="script_generate")
    async def generate_script(self, request: GenerateScriptRequest) -> ScriptResult:
        """台本を1ラウンド生成する。

        呼び出し順序は ADR-0013。**予約のcommitが外部呼び出しより前**であることが
        INV-15 の実体であり、`tests/unit/test_provider_reservations.py` が検査する。
        """
        async with self._session_factory() as session:
            episode = await EpisodeRepository(session).get(request.episode_id)
            if episode is None:
                raise UnreconciledReservationError(f"episode not found: {request.episode_id}")
            brief = await self._brief(
                session, request.episode_id, episode.topic, episode.topic_plan_id
            )
        topic = brief.topic
        template = script_prompt_template(brief.locale.locale)

        input_hash = script_input_hash(
            episode_id=request.episode_id,
            topic=topic,
            artifact_type=ArtifactType.SCRIPT.value,
            target_schema_version=SCRIPT_ARTIFACT_SCHEMA_VERSION,
            prompt_template_id=template.template_id,
            prompt_template_version=template.version,
            generator_id=f"{self._generator_id}:{self._model}",
            locale=brief.locale.locale,
            topic_plan_id=brief.topic_plan_id,
            content_profile=(
                f"{brief.content_profile.content_profile_id}@{brief.content_profile.version}"
            ),
        )

        # (1) 同じ入力の現行Artifactが既にあれば、生成器を呼ばずに返す（ADR-0012 / INV-17）
        async with self._session_factory() as session:
            existing = await ArtifactMetadataRepository(session).find_current(
                episode_id=request.episode_id,
                artifact_type=ArtifactType.SCRIPT,
                input_hash=input_hash,
            )
            if existing is not None:
                jobs = JobRepository(session)
                job = await jobs.get(request.job_id)
                if job is not None and job.status not in {
                    JobStatus.SKIPPED,
                    JobStatus.SUCCEEDED,
                    JobStatus.TERMINAL_FAILED,
                }:
                    await jobs.mark_skipped(request.job_id)
                    await session.commit()
                return ScriptResult(
                    artifact_id=existing.id,
                    bucket=existing.bucket,
                    object_key=existing.object_key,
                    sha256=existing.sha256,
                    schema_version=existing.schema_version,
                    reused=True,
                )

        async with self._session_factory() as session:
            await JobRepository(session).start(request.job_id)
            await session.commit()

        try:
            payload = await self._generate_round(
                request=request, brief=brief, input_hash=input_hash
            )
        except Exception as exc:
            await self._mark_job_failed(request.job_id, exc)
            raise

        return payload

    async def _brief(
        self, session: AsyncSession, episode_id: str, topic: str | None, topic_plan_id: str | None
    ) -> ScriptBrief:
        """plan があれば plan の profile と題材の列、無ければ legacy（ADR-0026）。

        plan が無くても現行の ``creative_brief`` Artifact があれば（agentic の Episode、
        ADR-0043 §6）その題材の列。plan のある Episode と、creative_brief の無い Episode は
        従来のまま。
        """
        if topic_plan_id is None:
            creative = await self._current_creative_brief(session, episode_id)
            if creative is not None:
                return creative_brief(
                    strategy_profile_id=creative.strategy_profile_id,
                    content_profile_id=creative.content_profile_id,
                    topic=creative.topic,
                    subject=creative.subject,
                    angle=creative.angle,
                    era=creative.era,
                    hook=creative.hook,
                )
            return legacy_brief(topic or "（トピック未指定）")
        plan = await TopicPlanRepository(session).get(topic_plan_id)
        if plan is None:
            raise PromptContractError(f"topic plan not found: {topic_plan_id}")
        return plan_brief(
            topic_plan_id=plan.id,
            strategy_profile_id=plan.strategy_profile_id,
            content_profile_id=plan.content_profile_id,
            # Episode.topic は plan の topic を写したもの。plan を正とする
            topic=plan.topic,
            subject=plan.subject,
            angle=plan.angle,
            era=plan.era,
            hook=plan.hook,
        )

    async def _current_creative_brief(
        self, session: AsyncSession, episode_id: str
    ) -> CreativeBrief | None:
        meta = await ArtifactMetadataRepository(session).find_current_by_type(
            episode_id, ArtifactType.CREATIVE_BRIEF
        )
        if meta is None:
            return None
        # 未知の schema 版は読み替えず拒否する（INV-54）。needs_input（再試行しない）
        try:
            brief = parse_artifact(
                ArtifactType.CREATIVE_BRIEF, await self._store.get_json(meta.object_key)
            )
        except UnsupportedArtifactSchemaError as exc:
            raise PromptContractError(str(exc)) from exc
        if not isinstance(brief, CreativeBrief):
            raise PromptContractError("current creative_brief is not a CreativeBrief")
        return brief

    async def _generate_round(
        self, *, request: GenerateScriptRequest, brief: ScriptBrief, input_hash: str
    ) -> ScriptResult:
        topic = brief.topic
        key = idempotency_key(
            provider=ProviderCall.CODEX_SCRIPT.value, input_hash=input_hash, round=request.round
        )
        # 新方式の Episode だけ実行記録を残す（ADR-0041）。legacy は読むだけで何も書かない
        capture = await self._capture_enabled(request.episode_id)

        # (2) evidence の無い予約が残っていれば、**新しい呼び出しを開始しない**（ADR-0013）
        async with self._session_factory() as session:
            reservations = ProviderReservationRepository(session)
            stale = [
                row
                for row in await reservations.find_unreconciled(
                    episode_id=request.episode_id, provider=ProviderCall.CODEX_SCRIPT
                )
                if row.idempotency_key != key
            ]
            if stale:
                # evidence の無い予約が残っている限り新しい呼び出しを開始しない。
                # 呼ばない・消さない・解放しない（ADR-0013 / INV-15）。
                raise UnreconciledReservationError(
                    f"unreconciled reservation {stale[0].id} blocks a new provider call"
                )
            reservation = await reservations.find_by_key(key)
            if reservation is None:
                # (3) 予約を **commit してから** 外部呼び出しへ進む（INV-15）
                reservation = await reservations.reserve(
                    episode_id=request.episode_id,
                    job_id=request.job_id,
                    provider=ProviderCall.CODEX_SCRIPT,
                    idempotency_key=key,
                    input_hash=input_hash,
                    round=request.round,
                )
                await session.commit()

        raw_key = f"{PROVIDER_RAW_PREFIX}/{request.episode_id}/{reservation.id}.txt"

        if reservation.raw_output_key is None:
            # (4) 起動直前に dispatched_at を commit（呼んだ証拠の第一段）
            async with self._session_factory() as session:
                await ProviderReservationRepository(session).mark_dispatched(reservation.id)
                await session.commit()

            generation = self._generation_request(request.episode_id, brief)
            try:
                result = await self._generator.generate(generation)
            except Exception as exc:
                # 呼び出しが**戻ってきた上で**失敗した。課金されたかは不明なので
                # 保守的に spent として確定する（ADR-0013 §保守的照合）。
                # こうしないと未照合が残り、次ラウンドが永久にブロックされて
                # 「retryable な失敗を安全に retry する」が成立しない。
                # 一方、Worker が落ちて**何も記録できなかった**場合は reserved の
                # まま残り、次ラウンドは正しくブロックされる。
                async with self._session_factory() as session:
                    await ProviderReservationRepository(session).mark_spent(
                        reservation.id,
                        raw_output_key=None,
                        reconciled_by="conservative",
                        failure_class=classify_failure(exc),
                        error_summary=f"{type(exc).__name__}: {exc}",
                    )
                    await session.commit()
                raise

            # (5) 生出力を保存し、**検証より前に** spent を確定する（ADR-0013）
            await self._store.put_text(raw_key, result.text)
            async with self._session_factory() as session:
                await ProviderReservationRepository(session).mark_spent(
                    reservation.id, raw_output_key=raw_key, reconciled_by="evidence"
                )
                await session.commit()
            raw_text = result.text
            generated_by_model = result.model
            raw_output_key: str = raw_key
            prompt_source = PromptSource.CAPTURED
            live_result: GenerationResult | None = result
        else:
            # 呼び出し後にcrashしていた場合。生出力が evidence なので再送しない
            raw_text = await self._store.get_text(reservation.raw_output_key)
            # 再開時は生出力しか無いので、設定側のモデル名で代替する。
            generated_by_model = self._model or DEFAULT_MODEL_LABEL
            raw_output_key = reservation.raw_output_key
            # 記録用に同じ入力・同じ版のテンプレートから組み直す（呼び直さない）
            generation = self._generation_request(request.episode_id, brief) if capture else None
            prompt_source = PromptSource.RE_RENDERED
            live_result = None

        # (6) ここで初めて検証する。失敗は retryable（ADR-0014）
        artifact = self._validate(
            raw_text,
            episode_id=request.episode_id,
            topic=topic,
            language=brief.locale.artifact_language,
            model=generated_by_model,
        )
        # (6b) スキーマの後に、ナレーションが尺に収まるかを決定論的に検査する（ADR-0026）
        _check_narration_budget(artifact, brief.locale)

        body = canonical_json_bytes(artifact)
        digest = sha256_hex(body)
        object_key = artifact_object_key(request.episode_id, ArtifactType.SCRIPT.value, digest)
        put = await self._store.put_json(object_key, artifact)

        run_capture: RunCapture | None = None
        stored: StoredSnapshot | None = None
        if capture:
            assert generation is not None
            run_capture = RunCapture(
                idempotency_key=f"agent_run:{WRITE_SCRIPT}:{reservation.id}",
                role=AgentRole.WRITER_DIRECTOR,
                step=WRITE_SCRIPT,
                model=self._model or DEFAULT_MODEL_LABEL,
                prompt_version=_prompt_version(brief),
                prompt=generation.prompt,
                prompt_source=prompt_source,
                input_refs={
                    "topic": topic,
                    "topic_plan_id": brief.topic_plan_id,
                    "locale": brief.locale.locale,
                    "content_profile": (
                        f"{brief.content_profile.content_profile_id}"
                        f"@{brief.content_profile.version}"
                    ),
                    "input_hash": input_hash,
                },
                request=request_document(generation),
                response=response_document(
                    live_result, raw_text=raw_text, raw_output_key=raw_output_key
                ),
                tool_invocations=(
                    reservation_invocation(
                        reservation.id,
                        provider=ProviderCall.CODEX_SCRIPT.value,
                        round=request.round,
                        idempotency_key=key,
                    ),
                ),
                episode_id=request.episode_id,
            )
            # 行より先に snapshot を保存する。失敗は Activity の失敗（記録の無い成果物を作らない）
            stored = await self._recorder.write(run_capture)

        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).record(
                episode_id=request.episode_id,
                artifact_type=ArtifactType.SCRIPT,
                schema_version=SCRIPT_ARTIFACT_SCHEMA_VERSION,
                bucket=self._bucket,
                object_key=put.key,
                sha256=put.sha256,
                size_bytes=put.size,
                produced_by_job_id=request.job_id,
                input_hash=input_hash,
            )
            await ProviderReservationRepository(session).attach_artifact(reservation.id, meta.id)
            await JobRepository(session).succeed(request.job_id)
            if run_capture is not None and stored is not None:
                # 成果物と同じ transaction（「成果物はあるが記録が無い」を作らない）
                run = await self._recorder.insert(
                    session, run_capture, stored, output_revision_id=meta.id
                )
                await record_generation_decision(
                    session,
                    episode_id=request.episode_id,
                    agent_run=run,
                    payload=generation_decision_payload(
                        decision_type=WRITE_SCRIPT,
                        question=f"Write the script for the topic {topic!r}",
                        candidate_summary=_script_summary(artifact),
                        result_revision=meta.id,
                        input_snapshot_ref=stored.snapshot_key,
                        tool_invocation_refs=tool_refs(run_capture.tool_invocations),
                    ),
                )
            await session.commit()

        return ScriptResult(
            artifact_id=meta.id,
            bucket=meta.bucket,
            object_key=meta.object_key,
            sha256=meta.sha256,
            schema_version=meta.schema_version,
            reused=False,
        )

    async def _capture_enabled(self, episode_id: str) -> bool:
        """新方式（mode != legacy）の Episode か。行が無ければ legacy（書かない）。"""
        async with self._session_factory() as session:
            control = await InterventionStore(session).get_control(episode_id)
        return control.mode is not InterventionMode.LEGACY

    def _generation_request(self, episode_id: str, brief: ScriptBrief) -> GenerationRequest:
        """生成器に渡す依頼。入力（brief）とテンプレートの版が同じなら同じ prompt になる。"""
        schema = ScriptArtifact.model_json_schema()
        duration_min, duration_max = brief.content_profile.script_duration_seconds
        prompt = render_localized_script_prompt(
            locale=brief.locale.locale,
            language=brief.locale.artifact_language,
            subject_matter_json=json.dumps(brief.subject_matter, ensure_ascii=False, indent=2),
            format_brief=brief.content_profile.format_brief,
            duration_min_seconds=duration_min,
            duration_max_seconds=duration_max,
            max_speech_units_per_second=brief.locale.max_speech_units_per_second,
            narration_budget_table=narration_budget_table(brief.locale),
            # プロンプトに埋めるスキーマとパース側のモデルを同じ1つから導出する
            # （生成側と取り込み側を分けない / AGENTS.md §8）。
            schema_json=json.dumps(schema, ensure_ascii=False, sort_keys=True),
        )
        return GenerationRequest(
            episode_id=episode_id,
            prompt=prompt,
            output_schema=schema,
            timeout_seconds=self._timeout_seconds,
        )

    def _validate(
        self, raw_text: str, *, episode_id: str, topic: str, language: str, model: str
    ) -> dict[str, object]:
        """生出力 → JSON → 契約検証。**修復はしない**（ADR-0014）。"""
        try:
            parsed = extract_json_object(raw_text)
        except ValueError as exc:
            raise ScriptOutputUnparseableError(str(exc)) from exc

        try:
            return build_script_artifact(
                episode_id=episode_id,
                # 言語は LLM の自己申告ではなく locale が決める（ADR-0026）。
                # 下流（音声・字幕・upload）はこの値で分岐する
                language=language,
                title=parsed.get("title", ""),
                hook=parsed.get("hook", ""),
                scenes=parsed.get("scenes", []),
                metadata={
                    "topic": topic,
                    "generator": self._generator_id,
                    # 実際に生成したモデルを記録する。設定でモデルを固定しない
                    # 運用（codex 自身の既定に従う）でも空にしない ── 空だと
                    # 契約違反で生成済みの台本が捨てられる（実環境で踏んだ）。
                    "generator_model": model or DEFAULT_MODEL_LABEL,
                },
            )
        except Exception as exc:  # pydantic ValidationError を含む
            raise ScriptSchemaViolationError(str(exc)[:1000]) from exc

    # ------------------------------------------------------------------ 失敗

    @activity.defn(name="script_record_failure")
    async def record_failure(self, request: RecordFailureRequest) -> FailureOutcome:
        from contracts.states import FailureClass

        failure_class = FailureClass(request.failure_class)
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.get(request.job_id)
            if job is not None and job.status not in {
                JobStatus.TERMINAL_FAILED,
                JobStatus.SUCCEEDED,
                JobStatus.SKIPPED,
            }:
                await jobs.record_failure(
                    request.job_id,
                    event=JobEvent.ATTEMPTS_EXHAUSTED
                    if job.status is JobStatus.RETRYABLE_FAILED
                    else job_event_for_failure(failure_class),
                    failure_class=failure_class,
                    error_summary=request.error_summary,
                )

            episodes = EpisodeRepository(session)
            episode = await episodes.apply_event(
                request.episode_id,
                episode_event_for_failure(failure_class),
                blocked_reason=f"{failure_class.value}: {request.error_summary}"[:1000],
            )
            if request.retry_exhausted and episode.status is EpisodeStatus.NEEDS_WORK:
                episode = await episodes.apply_event(
                    request.episode_id, EpisodeEvent.RETRY_BUDGET_EXHAUSTED
                )
            await session.commit()
            return FailureOutcome(episode_status=episode.status.value)

    async def _mark_job_failed(self, job_id: str, exc: BaseException) -> None:
        failure_class = classify_failure(exc)
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.get(job_id)
            if job is None or job.status in {
                JobStatus.TERMINAL_FAILED,
                JobStatus.SUCCEEDED,
                JobStatus.SKIPPED,
            }:
                return
            await jobs.record_failure(
                job_id,
                event=job_event_for_failure(failure_class),
                failure_class=failure_class,
                error_summary=f"{type(exc).__name__}: {exc}",
            )
            await session.commit()


def _prompt_version(brief: ScriptBrief) -> str:
    template = script_prompt_template(brief.locale.locale)
    return f"{template.template_id}@{template.version}"


def _script_summary(artifact: dict[str, object]) -> str:
    """判断記録の候補の要約（生成物そのものから。補完しない）。"""
    script = parse_script_artifact(artifact)
    return (
        f"title: {script.title}; hook: {script.hook}; "
        f"{len(script.scenes)} scenes; {script.total_duration_ms} ms"
    )


def _check_narration_budget(artifact: dict[str, object], locale: ScriptLocale) -> None:
    """各シーンのナレーションが ``locale`` の予算に収まること。超過は修復せず retryable。"""
    over = []
    for scene in parse_script_artifact(artifact).scenes:
        units = locale.count_speech_units(scene.narration)
        budget = locale.narration_budget(scene.duration_ms)
        if units > budget:
            over.append(
                f"{scene.id}: {units} {locale.speech_unit.value}s > budget {budget} "
                f"for {scene.duration_ms} ms"
            )
    if over:
        raise ScriptNarrationOverBudgetError(
            f"narration exceeds {locale.max_speech_units_per_second:g} "
            f"{locale.speech_unit.value}s/s ({locale.locale}): " + "; ".join(over)
        )


__all__ = [
    "PROVIDER_RAW_PREFIX",
    "CreateJobRequest",
    "EpisodeRef",
    "FailureOutcome",
    "GenerateScriptRequest",
    "PromptContractError",
    "RecordFailureRequest",
    "ScriptActivities",
    "ScriptResult",
]
