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

from contracts.artifacts import (
    SCRIPT_ARTIFACT_SCHEMA_VERSION,
    ScriptArtifact,
    build_script_artifact,
    extract_json_object,
)
from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType, ProviderCall
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.episode.transitions import EpisodeEvent
from domain.errors import (
    PromptContractError,
    ScriptOutputUnparseableError,
    ScriptSchemaViolationError,
    UnreconciledReservationError,
    classify_failure,
)
from domain.job.transitions import JobEvent, episode_event_for_failure, job_event_for_failure
from domain.script.identity import idempotency_key, script_input_hash
from domain.script.ports import GenerationRequest, StoryGenerator
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.storage.artifact_store import ArtifactStore
from prompts import PROMPT_TEMPLATE_ID, PROMPT_TEMPLATE_VERSION, render_script_prompt

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
        topic = episode.topic or "（トピック未指定）"

        input_hash = script_input_hash(
            episode_id=request.episode_id,
            topic=topic,
            artifact_type=ArtifactType.SCRIPT.value,
            target_schema_version=SCRIPT_ARTIFACT_SCHEMA_VERSION,
            prompt_template_id=PROMPT_TEMPLATE_ID,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            generator_id=f"{self._generator_id}:{self._model}",
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
                request=request, topic=topic, input_hash=input_hash
            )
        except Exception as exc:
            await self._mark_job_failed(request.job_id, exc)
            raise

        return payload

    async def _generate_round(
        self, *, request: GenerateScriptRequest, topic: str, input_hash: str
    ) -> ScriptResult:
        key = idempotency_key(
            provider=ProviderCall.CODEX_SCRIPT.value, input_hash=input_hash, round=request.round
        )

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

            schema = ScriptArtifact.model_json_schema()
            prompt = render_script_prompt(
                topic=topic,
                language="ja",
                # プロンプトに埋めるスキーマとパース側のモデルを同じ1つから導出する
                # （生成側と取り込み側を分けない / AGENTS.md §8）。
                schema_json=json.dumps(schema, ensure_ascii=False, sort_keys=True),
            )
            try:
                result = await self._generator.generate(
                    GenerationRequest(
                        episode_id=request.episode_id,
                        prompt=prompt,
                        output_schema=schema,
                        timeout_seconds=self._timeout_seconds,
                    )
                )
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
        else:
            # 呼び出し後にcrashしていた場合。生出力が evidence なので再送しない
            raw_text = await self._store.get_text(reservation.raw_output_key)
            # 再開時は生出力しか無いので、設定側のモデル名で代替する。
            generated_by_model = self._model or DEFAULT_MODEL_LABEL

        # (6) ここで初めて検証する。失敗は retryable（ADR-0014）
        artifact = self._validate(
            raw_text,
            episode_id=request.episode_id,
            topic=topic,
            model=generated_by_model,
        )

        body = canonical_json_bytes(artifact)
        digest = sha256_hex(body)
        object_key = artifact_object_key(request.episode_id, ArtifactType.SCRIPT.value, digest)
        put = await self._store.put_json(object_key, artifact)

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
            await session.commit()

        return ScriptResult(
            artifact_id=meta.id,
            bucket=meta.bucket,
            object_key=meta.object_key,
            sha256=meta.sha256,
            schema_version=meta.schema_version,
            reused=False,
        )

    def _validate(
        self, raw_text: str, *, episode_id: str, topic: str, model: str
    ) -> dict[str, object]:
        """生出力 → JSON → 契約検証。**修復はしない**（ADR-0014）。"""
        try:
            parsed = extract_json_object(raw_text)
        except ValueError as exc:
            raise ScriptOutputUnparseableError(str(exc)) from exc

        try:
            return build_script_artifact(
                episode_id=episode_id,
                language=parsed.get("language", "ja"),
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
