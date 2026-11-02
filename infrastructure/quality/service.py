"""品質ゲートの検査記録の読み書きと判定（ADR-0081 §2〜§4, §8, §9）。

``upload_gate``（自動）と ``POST /episodes/{id}/upload``（手動）が共有する唯一の入口。
flag OFF のときは呼ばれない（組み立ても行わない / INV-130）。

流れ: 現行 final_video を 1 回読む → 今の対象に結び付く有効な記録と使用宣言を選ぶ（純関数）→
必須なのに欠けるセットだけ producer で作って追記 → 記録から ``QualityReport`` を組み立てて判定 →
報告を保存。producer の失敗・保存の失敗は「記録が無い」になり、必須なら REVIEW（PASS に倒れない）。
報告を保存できなければ拒否。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.approval import ApproverKind
from contracts.artifacts import ScriptArtifact, parse_final_video, parse_script_artifact
from contracts.quality import (
    QUALITY_RULE_VERSION,
    QualityCheck,
    QualityGate,
    QualityReason,
    QualityVerdict,
)
from contracts.quality_records import CheckKey
from contracts.render import FinalVideoArtifact
from contracts.states import ArtifactType
from contracts.upload import (
    DescriptionBlockedError,
    InternalIdInDescriptionError,
    find_internal_ids,
    upload_marker,
)
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.quality.approval import UploadApprover, review_release_refusal
from domain.quality.deterministic import (
    QualityAssessment,
    QualityInputs,
    assess_for_auto_upload,
    assess_for_manual_upload,
    build_deterministic_report,
)
from domain.quality.inspection import RETRY_POLICY, RetryPolicy, backoff_delays, refine_hold_reason
from domain.quality.records import (
    CheckSetDraft,
    CheckSetInvalid,
    CheckSetProducer,
    CheckSubject,
    ProducerContext,
    recorded_checks_provider,
    required_keys,
    select_fresh_check_sets,
    select_usage,
)
from domain.quality.technical_qa_checks import technical_qa_checks
from domain.quality.upload_decision import UploadDecision
from domain.upload.keys import compute_upload_key
from domain.upload.metadata_builder import build_upload_metadata
from infrastructure.db.quality_repositories import (
    CurrentFinalVideo,
    QualityRecordRepository,
    SqlQualityRecordWriter,
)
from infrastructure.db.repositories import ArtifactMetadataRepository
from infrastructure.music.credits import BgmCreditSource
from infrastructure.quality.inspection import is_transient
from infrastructure.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

BUILTIN_PRODUCER = "quality-gate"
#: destination が未設定でも lint はできる（marker の値は lint の結果に影響しない: 行ごと除くため）
_UNCONFIGURED_DESTINATION = "unconfigured"


class ArtifactIntegrityError(RuntimeError):
    """保存物の正準 hash が対象 hash と一致しない・別 Episode のもの。記録の根拠にしない。"""


class _Context:
    """producer に渡す入力。final_video は正準 hash を対象 hash と照合してから返す。"""

    def __init__(
        self,
        subject: CheckSubject,
        current: CurrentFinalVideo,
        store: ArtifactStore,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._subject = subject
        self._current = current
        self._store = store
        self._session_factory = session_factory
        self._final: FinalVideoArtifact | None = None
        self._script: ScriptArtifact | None = None

    @property
    def subject(self) -> CheckSubject:
        return self._subject

    async def final_video(self) -> FinalVideoArtifact:
        if self._final is None:
            payload = await self._store.get_json(self._current.object_key)
            if sha256_hex(canonical_json_bytes(payload)) != self._subject.final_video_sha256:
                raise ArtifactIntegrityError("final_video body does not match its recorded sha256")
            artifact = parse_final_video(payload)
            if artifact.episode_id != self._subject.episode_id:
                raise ArtifactIntegrityError("final_video belongs to another episode")
            self._final = artifact
        return self._final

    async def script(self) -> ScriptArtifact:
        if self._script is None:
            ref = (await self.final_video()).source_script
            async with self._session_factory() as session:
                meta = await ArtifactMetadataRepository(session).get(ref.artifact_id)
            if (
                meta is None
                or meta.artifact_type is not ArtifactType.SCRIPT
                or meta.sha256 != ref.sha256
                or meta.episode_id != self._subject.episode_id
            ):
                raise ArtifactIntegrityError("the script of the final_video cannot be verified")
            payload = await self._store.get_json(meta.object_key)
            if sha256_hex(canonical_json_bytes(payload)) != ref.sha256:
                raise ArtifactIntegrityError("script body does not match its recorded sha256")
            self._script = parse_script_artifact(payload)
        return self._script


class TechnicalQaProducer:
    """final_video JSON 内の ``technical_qa`` を VISUAL / AUDIO の検査にする。"""

    key = CheckKey.TECHNICAL_QA

    async def produce(self, context: ProducerContext, *, now: datetime) -> CheckSetDraft | None:
        final = await context.final_video()
        return CheckSetDraft(
            key=self.key,
            checks=technical_qa_checks(final.technical_qa),
            producer=BUILTIN_PRODUCER,
            producer_version="technical-qa-1",
        )


class DescriptionLintProducer:
    """upload 工程と同じ組み立て口で作った説明欄に、内部 ID の lint を掛ける（INV-131）。"""

    key = CheckKey.DESCRIPTION_LINT

    def __init__(
        self, destination_id: str | None, credit_source: BgmCreditSource | None = None
    ) -> None:
        self._destination = destination_id or _UNCONFIGURED_DESTINATION
        self._credit_source = credit_source

    async def produce(self, context: ProducerContext, *, now: datetime) -> CheckSetDraft | None:
        final = await context.final_video()
        script = await context.script()
        upload_key = compute_upload_key(
            episode_id=context.subject.episode_id,
            final_video_sha256=final.media.sha256,
            destination_id=self._destination,
        )
        # upload 工程と同じ読み口（読めなければ例外 = 記録されない = REVIEW。ADR-0086 §5）
        credits = (
            await self._credit_source.credit_lines(
                context.subject.episode_id,
                final_video_sha256=context.subject.final_video_sha256,
                media_sha256=final.media.sha256,
            )
            if self._credit_source is not None
            else ()
        )
        clean = True
        try:
            metadata = build_upload_metadata(upload_key=upload_key, script=script, credits=credits)
            marker = upload_marker(upload_key)
            text = "\n".join(
                line for line in metadata.description.split("\n") if line.strip() != marker
            )
            clean = not find_internal_ids(text)
        except (DescriptionBlockedError, InternalIdInDescriptionError):
            clean = False
        check = QualityCheck(
            gate=QualityGate.METADATA,
            verdict=QualityVerdict.PASS if clean else QualityVerdict.BLOCK,
            reason_code=QualityReason.OK if clean else QualityReason.METADATA_LINT_FAILED,
            detail="description lint clean" if clean else "description lint failed",
            deterministic=True,
        )
        return CheckSetDraft(
            key=self.key,
            checks=(check,),
            producer=BUILTIN_PRODUCER,
            producer_version="description-lint-1",
        )


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """判定と、保存した報告の id（作れなかったとき ``None``）。"""

    assessment: QualityAssessment
    final_video_sha256: str | None = None
    report_id: str | None = None
    #: 一時障害を内部再試行で回復した回数（観測用。判定には使わない）
    recovered_faults: int = 0
    #: 再試行しても記録できなかった必須のセット（検査系の故障。品質の保留ではない）
    unavailable: tuple[str, ...] = ()


def _refused(reason: QualityReason, sha: str | None = None, *, recovered: int = 0) -> GateOutcome:
    return GateOutcome(QualityAssessment(UploadDecision(False, reason), None), sha, None, recovered)


class _Retrier:
    """1 回の ``evaluate`` の再試行の予算（ADR-0087 §8）。

    一時障害（``is_transient``）だけを、操作ごとに最大 ``policy.attempts`` 回、指数バックオフで
    再試行する。評価全体の経過が ``max_total_seconds`` を超えるなら待たずに諦める。
    恒久の失敗は 1 回で投げる（再試行しない）。時計と sleep は注入できる（実時間を待たない）。
    """

    def __init__(
        self,
        policy: RetryPolicy,
        sleep: Callable[[float], Awaitable[None]],
        monotonic: Callable[[], float],
    ) -> None:
        self._policy = policy
        self._delays = backoff_delays(policy)
        self._sleep = sleep
        self._monotonic = monotonic
        self._start = monotonic()
        self.recovered = 0

    async def run[T](self, label: str, operation: Callable[[], Awaitable[T]]) -> T:
        failures = 0
        while True:
            try:
                result = await operation()
            except Exception as exc:  # noqa: BLE001 - 分類して、一時障害だけ再試行する
                if not is_transient(exc):
                    raise
                failures += 1
                if failures >= self._policy.attempts:
                    raise
                delay = self._delays[failures - 1]
                elapsed = self._monotonic() - self._start
                if elapsed + delay > self._policy.max_total_seconds:
                    raise
                logger.info("quality inspection %s: transient fault, retry %d", label, failures)
                await self._sleep(delay)
                continue
            self.recovered += failures
            return result


class QualityGateService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        producers: Sequence[CheckSetProducer] = (),
        destination_id: str | None = None,
        credit_source: BgmCreditSource | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        retry_policy: RetryPolicy = RETRY_POLICY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._retry_policy = retry_policy
        self._sleep = sleep
        self._monotonic = monotonic
        self._session_factory = session_factory
        self._store = store
        self._clock = clock
        self._writer = SqlQualityRecordWriter(session_factory)
        builtin: list[CheckSetProducer] = [
            TechnicalQaProducer(),
            DescriptionLintProducer(destination_id, credit_source),
        ]
        by_key = {p.key: p for p in builtin}
        for producer in producers:
            if producer.key in by_key:
                raise ValueError(f"producer for {producer.key.value} is already registered")
            by_key[producer.key] = producer
        self._producers = by_key

    async def evaluate(self, episode_id: str, *, manual: bool) -> GateOutcome:
        """どんな失敗も PASS にしない。例外は呼び出し側で拒否に倒される前にここで拒否にする。

        検査系の一時障害は内側で上限つきに再試行する（ADR-0087 §8）。それでも読めない・書けない
        ものは拒否のままで、理由だけ ``INSPECTION_UNAVAILABLE`` として品質の保留と区別する。
        """
        retrier = _Retrier(self._retry_policy, self._sleep, self._monotonic)
        try:
            return await self._evaluate(episode_id, manual=manual, retrier=retrier)
        except Exception as exc:  # noqa: BLE001 - fail-closed
            logger.warning("quality gate failed for episode=%s", episode_id, exc_info=True)
            reason = (
                QualityReason.INSPECTION_UNAVAILABLE
                if is_transient(exc)
                else QualityReason.REPORT_EVALUATION_ERROR
            )
            return _refused(reason, recovered=retrier.recovered)

    async def _evaluate(self, episode_id: str, *, manual: bool, retrier: _Retrier) -> GateOutcome:
        async def read_current() -> CurrentFinalVideo | None:
            async with self._session_factory() as session:
                return await QualityRecordRepository(session).current_final_video(episode_id)

        current = await retrier.run("current_final_video", read_current)
        if current is None:
            return _refused(QualityReason.REPORT_PRODUCTION_FAILED, recovered=retrier.recovered)
        try:
            subject = CheckSubject(episode_id, current.final_video_sha256, current.input_hash)
        except CheckSetInvalid:
            return _refused(
                QualityReason.REPORT_PRODUCTION_FAILED,
                current.final_video_sha256,
                recovered=retrier.recovered,
            )
        now = self._clock()
        context = _Context(subject, current, self._store, self._session_factory)
        fresh, usage = await self._load(subject, now, retrier)
        unavailable: set[CheckKey] = set()
        for key in required_keys(usage):
            if key in fresh.fresh:
                continue
            if not await self._produce_and_write(key, context, subject, now, retrier):
                unavailable.add(key)
        fresh, usage = await self._load(subject, now, retrier)
        inputs = QualityInputs(episode_id, subject.final_video_sha256, subject.input_hash)
        providers = (recorded_checks_provider(fresh, usage, unavailable=frozenset(unavailable)),)
        kwargs = {"now": now, "rule_version": QUALITY_RULE_VERSION, "providers": providers}
        report = build_deterministic_report(inputs, **kwargs)
        assess = assess_for_manual_upload if manual else assess_for_auto_upload
        raw = assess(inputs, **kwargs)
        assessment = QualityAssessment(
            refine_hold_reason(raw.decision, report.checks), raw.verdict, raw.review_approved
        )

        async def save() -> str:
            async with self._session_factory() as session:
                report_id = await QualityRecordRepository(session).save_report(report)
                await session.commit()
                return report_id

        try:
            report_id = await retrier.run("save_report", save)
        except Exception as exc:  # noqa: BLE001 - 監査が残らない許可は出さない（INV-128）
            logger.warning(
                "quality report could not be saved episode=%s", episode_id, exc_info=True
            )
            reason = (
                QualityReason.INSPECTION_UNAVAILABLE
                if is_transient(exc)
                else QualityReason.REPORT_PRODUCTION_FAILED
            )
            return _refused(reason, subject.final_video_sha256, recovered=retrier.recovered)
        return GateOutcome(
            assessment,
            subject.final_video_sha256,
            report_id,
            retrier.recovered,
            tuple(sorted(k.value for k in unavailable)),
        )

    async def _load(self, subject: CheckSubject, now: datetime, retrier: _Retrier):  # type: ignore[no-untyped-def]
        async def read():  # type: ignore[no-untyped-def]
            async with self._session_factory() as session:
                repo = QualityRecordRepository(session)
                return await repo.list_check_sets(subject), await repo.list_usage(subject)

        records, declarations = await retrier.run("load_records", read)
        fresh = select_fresh_check_sets(
            records, subject, rule_version=QUALITY_RULE_VERSION, now=now
        )
        return fresh, select_usage(declarations, subject)

    async def _produce_and_write(
        self,
        key: CheckKey,
        context: _Context,
        subject: CheckSubject,
        now: datetime,
        retrier: _Retrier,
    ) -> bool:
        """記録できたか、品質の理由で記録しない（恒久）なら True。検査系の一時障害で記録できな
        かったときだけ False（呼び出し側が ``INSPECTION_UNAVAILABLE`` にする）。
        """
        producer = self._producers.get(key)
        if producer is None:
            return True  # 登録が無い = 記録されない = 必須欠落で REVIEW（本物の欠落）
        try:
            draft = await retrier.run(
                f"produce:{key.value}", lambda: producer.produce(context, now=now)
            )
            if draft is None or draft.key is not key:
                return True
            await retrier.run(
                f"write:{key.value}",
                lambda: self._writer.write_check_set(
                    subject, draft, checked_at=now, rule_version=QUALITY_RULE_VERSION
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 失敗は「記録が無い」になる
            logger.warning(
                "quality check %s could not be produced for episode=%s (%s)",
                key.value,
                subject.episode_id,
                "transient" if is_transient(exc) else "permanent",
                exc_info=True,
            )
            return not is_transient(exc)
        return True

    async def record_manual_approval(
        self, outcome: GateOutcome, *, episode_id: str, approver: UploadApprover
    ) -> str:
        """承認の記録（誰・種別・いつ・対象 hash・報告）を書く。書けなければ例外。

        呼び出し側は例外のとき workflow を起動しない。
        """
        refusal = review_release_refusal(approver)
        if refusal is not None:
            raise PermissionError(refusal.value)
        if outcome.report_id is None or outcome.final_video_sha256 is None:
            raise LookupError("no saved report to approve")
        async with self._session_factory() as session:
            approval_id = await QualityRecordRepository(session).save_manual_approval(
                episode_id=episode_id,
                report_id=outcome.report_id,
                final_video_sha256=outcome.final_video_sha256,
                approver_kind=ApproverKind(approver.kind),
                approver_id=approver.approver_id,
                approved_at=self._clock(),
            )
            await session.commit()
        return approval_id
