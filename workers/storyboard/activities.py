"""Storyboard Worker の Activity 群（ADR-0015）。

Activity は「入力から出力Artifactを作って結果を返す」だけ。
**次に何をするかは決めない**（INV-4）。他のworkerをimportしない（INV-3）。

有料（外部AI）呼び出しの順序は ADR-0013 が権威:
予約をcommit → dispatchをcommit → 呼ぶ → 生出力保存 → spentをcommit → 解釈・検証 → Artifact。
台本 worker と小さなオーケストレーションが重複するのは意図的（worker 間 import を避ける）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from contracts.artifacts import (
    STORYBOARD_ARTIFACT_SCHEMA_VERSION,
    ScriptArtifact,
    build_storyboard_artifact,
    parse_storyboard_artifact,
)
from contracts.states import (
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobStatus,
    JobType,
    ProviderCall,
    ReservationStatus,
)
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.episode.transitions import EpisodeEvent
from domain.errors import (
    ArtifactConflictError,
    ProviderInvocationError,
    StoryboardInputInvalidError,
    StoryboardInputMissingError,
    StoryboardSchemaViolationError,
    UnreconciledReservationError,
    classify_failure,
)
from domain.job.transitions import JobEvent, episode_event_for_failure, job_event_for_failure
from domain.storyboard.coverage import check_storyboard_covers_script
from domain.storyboard.identity import idempotency_key, storyboard_input_hash
from domain.storyboard.normalize import assign_scene_identity, normalize_timeline
from domain.storyboard.ports import StoryboardGenerator, StoryboardRequest
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservation,
    ProviderReservationRepository,
)
from infrastructure.storage.artifact_store import ArtifactStore

#: 生出力の置き場所。Artifact ではない（スキーマ検証を通らないため / ADR-0013）。
PROVIDER_RAW_PREFIX = "provider-raw"

#: モデル名が得られないときに記録する値。空文字にしない。
DEFAULT_MODEL_LABEL = "codex-config-default"

#: storyboard 工程へ入ってよい Episode 状態（駐機点。ADR-0011 / ADR-0015）。
ADMISSIBLE_STATUSES = frozenset({EpisodeStatus.SCRIPT_READY, EpisodeStatus.STORYBOARD_READY})

_JOB_DONE = frozenset({JobStatus.SUCCEEDED, JobStatus.SKIPPED, JobStatus.TERMINAL_FAILED})


@dataclass
class EpisodeRef:
    episode_id: str


@dataclass
class AdmitRequest:
    episode_id: str
    #: 入場を主張する workflow。``in_progress`` の再入場は同じ workflow にだけ許す。
    workflow_id: str


@dataclass
class AdmitResult:
    #: False なら workflow は何もせず終わる（状態も Job も作らない）。
    admitted: bool
    #: 判定時点の Episode 状態。Episode が無ければ空文字。
    status: str


@dataclass
class CreateJobRequest:
    episode_id: str
    max_attempts: int


@dataclass
class GenerateStoryboardRequest:
    episode_id: str
    job_id: str
    round: int


@dataclass
class StoryboardResult:
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


@dataclass(frozen=True)
class _LoadedScript:
    meta: ArtifactMetadata
    script: ScriptArtifact


class StoryboardActivities:
    """外部依存をすべて注入する。テストは実サービスなしで同じコードパスを走らせる（INV-18）。

    ``prompt_template_id`` / ``prompt_template_version`` は ``input_hash`` の構成要素。
    値の定義元は ``prompts``（run_worker が配線する）。ここでは受け取るだけ。
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        generator: StoryboardGenerator,
        bucket: str,
        timeout_seconds: int,
        prompt_template_id: str,
        prompt_template_version: str,
        model_label: str = DEFAULT_MODEL_LABEL,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._generator = generator
        self._bucket = bucket
        self._timeout_seconds = timeout_seconds
        self._prompt_template_id = prompt_template_id
        self._prompt_template_version = prompt_template_version
        self._model_label = model_label or DEFAULT_MODEL_LABEL

    def all_activities(self) -> Sequence[Callable[..., object]]:
        """Workerへ登録するActivity。ここが登録漏れの唯一の防波堤。"""
        return [
            self.admit_episode,
            self.create_job,
            self.generate_storyboard,
            self.mark_ready,
            self.record_failure,
        ]

    # ------------------------------------------------------------------ 状態

    @activity.defn(name="storyboard_admit_episode")
    async def admit_episode(self, request: AdmitRequest) -> AdmitResult:
        """駐機点から工程へ入れる。

        - ``script_ready`` / ``storyboard_ready`` → ``stage_admitted`` で ``in_progress``。
          同じトランザクションで ``episodes.workflow_id`` に入場した workflow を記録する
        - ``in_progress`` → **記録された workflow_id が自分と一致する場合だけ**何もせず通す
          （Activity 再実行で二重遷移しない / INV-17）。他の workflow（例: 実行中の台本工程）
          が ``in_progress`` にしている Episode へは入らない。入ると台本未完成のまま
          needs_input で失敗し、他工程の Episode を ``blocked`` へ落としてしまう
        - それ以外（blocked / needs_work / planned / 終端 / 不在）→ 入れない。
          失敗として記録もしない: 工程に入っていないので job も Episode 事象も無い。
          ``blocked`` からの再開は ``resumed``（人間の判断）であり、この API の責務ではない。

        workflow_id は相関用の列（INV-8）だが、ここでは PostgreSQL 上の入場記録として読む。
        権威は依然 PostgreSQL であり Temporal の実行状態ではない。
        """
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            episode = await episodes.get(request.episode_id)
            if episode is None:
                return AdmitResult(admitted=False, status="")
            if episode.status is EpisodeStatus.IN_PROGRESS:
                owner = await episodes.get_workflow_id(request.episode_id)
                return AdmitResult(
                    admitted=owner == request.workflow_id, status=episode.status.value
                )
            if episode.status not in ADMISSIBLE_STATUSES:
                return AdmitResult(admitted=False, status=episode.status.value)
            updated = await episodes.apply_event(request.episode_id, EpisodeEvent.STAGE_ADMITTED)
            await episodes.set_workflow_id(request.episode_id, request.workflow_id)
            await session.commit()
            return AdmitResult(admitted=True, status=updated.status.value)

    @activity.defn(name="storyboard_create_job")
    async def create_job(self, request: CreateJobRequest) -> str:
        """非終端の PLAN_STORYBOARD job があれば再利用、無ければ作る。

        台本 worker と違い終端 job は再利用しない: storyboard は同じ Episode に対して
        何度も起動される（台本の更新ごと）ので、1回の起動 = 1 job として履歴を残す。
        """
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            for job in await jobs.list_for_episode(request.episode_id):
                if job.type is JobType.PLAN_STORYBOARD and job.status not in _JOB_DONE:
                    return job.id  # Activityの再実行でJobを重複生成しない
            job = await jobs.create(
                episode_id=request.episode_id,
                type=JobType.PLAN_STORYBOARD,
                max_attempts=request.max_attempts,
            )
            await session.commit()
            return job.id

    @activity.defn(name="storyboard_mark_ready")
    async def mark_ready(self, request: EpisodeRef) -> str:
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            current = await episodes.get(request.episode_id)
            if current is not None and current.status is EpisodeStatus.STORYBOARD_READY:
                return current.status.value  # 再実行で二重遷移しない（INV-17）
            episode = await episodes.apply_event(request.episode_id, EpisodeEvent.STORYBOARD_READY)
            await session.commit()
            return episode.status.value

    # ------------------------------------------------------------------ 生成

    @activity.defn(name="storyboard_generate")
    async def generate_storyboard(self, request: GenerateStoryboardRequest) -> StoryboardResult:
        """storyboard を1ラウンド生成する。失敗時は job を失敗にしてから再送出する。"""
        try:
            return await self._generate(request)
        except Exception as exc:
            await self._mark_job_failed(request.job_id, exc)
            raise

    async def _generate(self, request: GenerateStoryboardRequest) -> StoryboardResult:
        loaded = await self._load_script(request.episode_id)
        input_hash = storyboard_input_hash(
            episode_id=request.episode_id,
            artifact_type=ArtifactType.STORYBOARD.value,
            target_schema_version=STORYBOARD_ARTIFACT_SCHEMA_VERSION,
            script_sha256=loaded.meta.sha256,
            prompt_template_id=self._prompt_template_id,
            prompt_template_version=self._prompt_template_version,
            generator_id=self._generator.generator_id,
            generation_spec_id=self._generator.generation_spec_id,
        )

        # (1) 同じ入力の現行Artifactが既にあれば、生成器を呼ばずに返す（ADR-0012 / INV-17）
        async with self._session_factory() as session:
            existing = await ArtifactMetadataRepository(session).find_current(
                episode_id=request.episode_id,
                artifact_type=ArtifactType.STORYBOARD,
                input_hash=input_hash,
            )
            if existing is not None:
                jobs = JobRepository(session)
                job = await jobs.get(request.job_id)
                if job is not None and job.status not in _JOB_DONE:
                    await jobs.mark_skipped(request.job_id)
                    await session.commit()
                return _result(existing, reused=True)

        async with self._session_factory() as session:
            await JobRepository(session).start(request.job_id)
            await session.commit()

        return await self._generate_round(request=request, loaded=loaded, input_hash=input_hash)

    async def _load_script(self, episode_id: str) -> _LoadedScript:
        """現行の台本 Artifact を読み、sha256 と契約で検証する。"""
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).find_current_by_type(
                episode_id, ArtifactType.SCRIPT
            )
        if meta is None:
            raise StoryboardInputMissingError(f"no current script artifact for {episode_id}")
        try:
            payload = await self._store.get_json(meta.object_key)
        except Exception as exc:
            raise StoryboardInputInvalidError(
                f"script artifact {meta.object_key} is not readable: {type(exc).__name__}"
            ) from exc
        digest = sha256_hex(canonical_json_bytes(payload))
        if digest != meta.sha256:
            raise StoryboardInputInvalidError(
                f"script artifact sha256 mismatch: stored={digest} metadata={meta.sha256}"
            )
        try:
            script = ScriptArtifact.model_validate(payload)
        except Exception as exc:  # pydantic ValidationError を含む
            raise StoryboardInputInvalidError(f"script artifact invalid: {str(exc)[:500]}") from exc
        return _LoadedScript(meta=meta, script=script)

    async def _generate_round(
        self, *, request: GenerateStoryboardRequest, loaded: _LoadedScript, input_hash: str
    ) -> StoryboardResult:
        provider = ProviderCall.CODEX_STORYBOARD
        key = idempotency_key(provider=provider.value, input_hash=input_hash, round=request.round)
        raw_key_prefix = f"{PROVIDER_RAW_PREFIX}/{request.episode_id}"

        # (2) evidence の無い予約が残っていれば、**新しい呼び出しを開始しない**（ADR-0013）
        async with self._session_factory() as session:
            reservations = ProviderReservationRepository(session)
            stale = [
                row
                for row in await reservations.find_unreconciled(
                    episode_id=request.episode_id, provider=provider
                )
                if row.idempotency_key != key
            ]
            if stale:
                raise UnreconciledReservationError(
                    f"unreconciled reservation {stale[0].id} blocks a new provider call"
                )
            reservation = await reservations.find_by_key(key)
            if reservation is None:
                # (3) 予約を **commit してから** 外部呼び出しへ進む（INV-15）
                reservation = await reservations.reserve(
                    episode_id=request.episode_id,
                    job_id=request.job_id,
                    provider=provider,
                    idempotency_key=key,
                    input_hash=input_hash,
                    round=request.round,
                )
                await session.commit()

        raw_key = f"{raw_key_prefix}/{reservation.id}.txt"
        raw_text, model = await self._raw_output(
            request=request, loaded=loaded, reservation=reservation, raw_key=raw_key
        )

        # (6) ここで初めて解釈・検証する。失敗は retryable（ADR-0014）
        artifact = self._build(raw_text, loaded=loaded, episode_id=request.episode_id, model=model)

        body = canonical_json_bytes(artifact)
        digest = sha256_hex(body)
        object_key = artifact_object_key(request.episode_id, ArtifactType.STORYBOARD.value, digest)
        put = await self._store.put_json(object_key, artifact)

        # (7) 読み戻して正規形の sha256 を照合する。一致しない保存物を「現行」にしない。
        readback = await self._store.get_json(put.key)
        readback_sha = sha256_hex(canonical_json_bytes(readback))
        if readback_sha != digest or put.sha256 != digest:
            # ストレージの完全性の問題で、LLM の揺れではない。ラウンドを重ねても
            # 課金が増えるだけなので retryable にしない。専用の分類を持たない
            # ArtifactConflictError は未分類 → needs_input（INV-12）で人間へ回る。
            raise ArtifactConflictError(
                f"storyboard readback sha256 mismatch at {put.key}: "
                f"expected={digest} put={put.sha256} readback={readback_sha}"
            )

        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).record(
                episode_id=request.episode_id,
                artifact_type=ArtifactType.STORYBOARD,
                schema_version=STORYBOARD_ARTIFACT_SCHEMA_VERSION,
                bucket=self._bucket,
                object_key=put.key,
                sha256=digest,
                size_bytes=put.size,
                produced_by_job_id=request.job_id,
                input_hash=input_hash,
            )
            await ProviderReservationRepository(session).attach_artifact(reservation.id, meta.id)
            await JobRepository(session).succeed(request.job_id)
            await session.commit()

        return _result(meta, reused=False)

    async def _raw_output(
        self,
        *,
        request: GenerateStoryboardRequest,
        loaded: _LoadedScript,
        reservation: ProviderReservation,
        raw_key: str,
    ) -> tuple[str, str]:
        """生出力を得る。ADR-0013 の再開分岐表に従い、**呼ぶのは未 dispatch のときだけ**。"""
        if reservation.raw_output_key is not None:
            # spent + 生出力あり: 呼び出し後に解釈・保存の前で落ちていた。再送しない。
            return await self._store.get_text(reservation.raw_output_key), self._model_label

        if reservation.status is not ReservationStatus.RESERVED:
            # spent/abandoned + 生出力なし: このラウンドは消費済み。次ラウンドは workflow が決める。
            raise ProviderInvocationError(
                f"round {request.round} already consumed without raw output "
                f"(reservation {reservation.id})"
            )

        if reservation.dispatched_at is not None:
            # 生出力の保存と spent の commit の間で落ちた場合は、保存済みの生出力が evidence。
            if await self._store.exists(raw_key):
                async with self._session_factory() as session:
                    await ProviderReservationRepository(session).mark_spent(
                        reservation.id, raw_output_key=raw_key, reconciled_by="evidence"
                    )
                    await session.commit()
                return await self._store.get_text(raw_key), self._model_label
            # 曖昧（呼んだか分からない）。呼ばない・消さない・解放しない。
            raise UnreconciledReservationError(
                f"reservation {reservation.id} was dispatched without evidence"
            )

        # (4) 起動直前に dispatched_at を commit（呼んだ証拠の第一段）
        async with self._session_factory() as session:
            await ProviderReservationRepository(session).mark_dispatched(reservation.id)
            await session.commit()

        try:
            raw = await self._generator.generate(
                StoryboardRequest(
                    episode_id=request.episode_id,
                    job_id=request.job_id,
                    script=loaded.script,
                    timeout_seconds=self._timeout_seconds,
                )
            )
        except Exception as exc:
            # 戻ってきた上での失敗。課金されたかは不明なので保守的に spent（ADR-0013）。
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

        # (5) 生出力を保存し、**解釈より前に** spent を確定する（ADR-0013）
        await self._store.put_text(raw_key, raw.text)
        async with self._session_factory() as session:
            await ProviderReservationRepository(session).mark_spent(
                reservation.id, raw_output_key=raw_key, reconciled_by="evidence"
            )
            await session.commit()
        return raw.text, raw.model or self._model_label

    def _build(
        self, raw_text: str, *, loaded: _LoadedScript, episode_id: str, model: str
    ) -> dict[str, object]:
        """生出力 → 下書き → 時間軸正規化 → 採番 → 契約 → 台本カバレッジ。修復はしない。

        システムが決める値（episode_id / type / schema_version / source_script /
        total_duration_ms / metadata / scene_id / order）はここで注入する。生成器に決めさせない。
        """
        script = loaded.script
        drafts = self._generator.interpret(raw_text, script)
        normalized = normalize_timeline(drafts, script.total_duration_ms)
        scenes = assign_scene_identity(normalized)
        try:
            artifact = build_storyboard_artifact(
                episode_id=episode_id,
                source_script={
                    "artifact_id": loaded.meta.id,
                    "sha256": loaded.meta.sha256,
                    "schema_version": loaded.meta.schema_version,
                },
                scenes=scenes,
                total_duration_ms=script.total_duration_ms,
                metadata={
                    "generator": self._generator.generator_id,
                    "generator_model": model or self._model_label,
                    "generation_spec_id": self._generator.generation_spec_id,
                },
            )
        except Exception as exc:  # pydantic ValidationError を含む
            raise StoryboardSchemaViolationError(str(exc)[:1000]) from exc
        check_storyboard_covers_script(parse_storyboard_artifact(artifact), script)
        return artifact

    # ------------------------------------------------------------------ 失敗

    @activity.defn(name="storyboard_record_failure")
    async def record_failure(self, request: RecordFailureRequest) -> FailureOutcome:
        failure_class = FailureClass(request.failure_class)
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.get(request.job_id)
            if job is not None and job.status not in _JOB_DONE:
                await jobs.record_failure(
                    request.job_id,
                    event=JobEvent.ATTEMPTS_EXHAUSTED
                    if job.status is JobStatus.RETRYABLE_FAILED
                    else job_event_for_failure(failure_class),
                    failure_class=failure_class,
                    error_summary=request.error_summary,
                )

            episodes = EpisodeRepository(session)
            current = await episodes.get(request.episode_id)
            if current is not None and current.status is not EpisodeStatus.IN_PROGRESS:
                # 再実行で二重遷移しない（INV-17）。既に blocked 等へ落ちている。
                await session.commit()
                return FailureOutcome(episode_status=current.status.value)
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
            if job is None or job.status in _JOB_DONE:
                return
            if job.status is JobStatus.RETRYABLE_FAILED and failure_class in {
                FailureClass.TRANSIENT,
                FailureClass.RETRYABLE,
            }:
                return  # start 前の失敗が同じ分類で重なっただけ。表に辺が無いので書かない。
            await jobs.record_failure(
                job_id,
                event=job_event_for_failure(failure_class),
                failure_class=failure_class,
                error_summary=f"{type(exc).__name__}: {exc}",
            )
            await session.commit()


def _result(meta: ArtifactMetadata, *, reused: bool) -> StoryboardResult:
    return StoryboardResult(
        artifact_id=meta.id,
        bucket=meta.bucket,
        object_key=meta.object_key,
        sha256=meta.sha256,
        schema_version=meta.schema_version,
        reused=reused,
    )


__all__ = [
    "ADMISSIBLE_STATUSES",
    "PROVIDER_RAW_PREFIX",
    "AdmitResult",
    "CreateJobRequest",
    "EpisodeRef",
    "FailureOutcome",
    "GenerateStoryboardRequest",
    "RecordFailureRequest",
    "StoryboardActivities",
    "StoryboardResult",
]
