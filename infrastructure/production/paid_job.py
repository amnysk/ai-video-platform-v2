"""非同期ジョブ型の有料呼び出しの INV-15 オーケストレーション（ADR-0013 / ADR-0017 §3）。

画像・動画の worker が共有する（worker 間 import を避けるため infrastructure に置く / INV-3）。
メディア固有の検証・正規化・Artifact 化は呼び出し側が行う。

書き込み順序（ADR-0017 §3）:

submit: 再利用確認 → 台帳からラウンドを決める → 未照合確認 → reserve **commit** →
        dispatched **commit** → submit → provider job 参照 **commit**

台帳のラウンド（ADR-0017 §3）: ``PaidJobSpec.round`` は workflow の run ごとの試行番号にすぎず、
冪等キーには使わない。同じ入力の最新の予約から導く:

- 無い → 1
- ``reserved`` → その予約を再開
  （参照あり: await / dispatch 済み参照なし: 人手照合 / 未 dispatch: そのまま進む）
- Artifact が紐づいている、または evidence があり失敗の記録が無い → その予約を await で再開
- それ以外（取得物なしで spent・検証に落ちた evidence・abandoned）→ 最新ラウンド + 1

同じラウンドの並行 INSERT は ``idempotency_key`` の一意制約で片方が落ちるので、読み直して再開する。
await:  参照を台帳から読む → poll（heartbeat）→ download → 生の取得物を ``provider-raw/`` へ →
        ``mark_spent(evidence)`` **commit** → 呼び出し側へ返す（検証はその後）

heartbeat: poll の各回に加え、download・evidence 保存・spent の commit の間は背景タスクが
``heartbeat_interval_seconds`` ごとに送る（1つの長い await で heartbeat timeout を越えない）。
並行した別の await 試行（heartbeat 切れの旧試行）が同じ evidence で先に spent にしていたら、
それを受け入れて続ける（紐づいた Artifact があればそれを返す）。

submit の失敗:

- ``ProviderSubmitAmbiguousError`` / cancel: 予約は ``reserved`` + dispatched + 参照なしで残す
  （曖昧の証拠。人手照合まで同じシーンの新ラウンドを止める）
- adapter が「受理されなかった」と分類した例外（``NOT_ACCEPTED_SUBMIT_ERRORS``: 接続前の失敗・
  429・4xx）だけ: ADR-0013 の「戻ってきた上での失敗」なので
  ``spent`` + ``reconciled_by="conservative"``。
  台帳に「受理されなかった」辺は無く、``abandoned`` は人手専用なので、課金された前提で確定して
  次ラウンドを進める（安全側。見積もり額は過大計上になりうる / ADR-0017 負債）
- それ以外の例外はすべて曖昧扱い（``ProviderSubmitAmbiguousError`` として送出し、予約は残す）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.production_activities import (
    AUTH_INCIDENT_SUPPRESSION_THRESHOLD,
    AUTH_INCIDENT_WINDOW_MINUTES,
)
from contracts.states import ArtifactType, ProviderCall, ReservationStatus
from domain.artifact.entities import ArtifactMetadata
from domain.errors import (
    InvalidTransitionError,
    MediaValidationError,
    ProviderCredentialSuspectedOutageError,
    ProviderJobFailedError,
    ProviderPollDeadlineError,
    ProviderRejectedError,
    ProviderSubmitAmbiguousError,
    ProviderUnavailableError,
    UnreconciledReservationError,
    classify_failure,
)
from domain.production.identity import idempotency_key
from domain.production.ports import JobFailed, JobPending, JobStatus, ProviderJobRef
from infrastructure.artifact.verify import ArtifactVerdict, find_and_verify_current, verify_artifact
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    ProviderAuthIncidentRepository,
    ProviderReservation,
    ProviderReservationRepository,
)
from infrastructure.storage.artifact_store import ArtifactStore
from infrastructure.workdir import WorkDirectory

logger = logging.getLogger(__name__)

PROVIDER_RAW_PREFIX = "provider-raw"
_COST_QUANTUM = Decimal("0.0001")
#: 長い単発の await（download / 保存 / commit）の間に送る heartbeat の間隔（秒）
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 20.0
#: 同じラウンドの並行 INSERT に負けたときに読み直す回数
_RESERVE_ATTEMPTS = 3
#: submit がこれらを投げたら「受理されていない」と adapter が示している（fal_queue の分類）。
#: ここに無い例外は受理されたか分からないものとして扱う（再送しない・消さない）。
NOT_ACCEPTED_SUBMIT_ERRORS: tuple[type[Exception], ...] = (
    ProviderJobFailedError,
    ProviderRejectedError,
    ProviderUnavailableError,
)


class AsyncJobGenerator(Protocol):
    """``ImageGenerator`` / ``VideoGenerator`` の共通部分（request 型は問わない）。"""

    @property
    def generator_id(self) -> str: ...

    def estimate_cost_usd(self, request: Any) -> float: ...

    async def submit(self, request: Any) -> ProviderJobRef: ...

    async def poll(self, ref: ProviderJobRef) -> JobStatus: ...

    async def download(self, ref: ProviderJobRef, dest: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class PaidJobSpec:
    """1ラウンドの有料呼び出しの同一性。"""

    episode_id: str
    scene_id: str
    provider: ProviderCall
    artifact_type: ArtifactType
    input_hash: str
    #: workflow の run ごとの試行番号（ログ用）。**台帳のラウンドではない**（台帳から導く）
    round: int
    job_id: str | None = None
    #: 再利用の完全性検証（ADR-0033）が「現在有効な生成設定版」として使う値。
    #: ``generator.generation_profile_id`` そのままとは限らない ── video のように
    #: generator + 付随パラメータ（motion profile 等）を合成した値を Activity 側が持つ場合は、
    #: その合成済みの値をここへ渡す（``PaidJobRunner`` は provider 固有の合成方法を知らない）。
    #: 省略時はこの型のチェックを行わない。
    current_generation_profile_id: str | None = None

    def key_for_round(self, ledger_round: int) -> str:
        return idempotency_key(
            provider=self.provider.value, input_hash=self.input_hash, round=ledger_round
        )

    @property
    def idempotency_key(self) -> str:
        """台帳ラウンド1の冪等キー（テスト・照合用）。実際のキーは ``key_for_round``。"""
        return self.key_for_round(1)


@dataclass(frozen=True, slots=True)
class Reused:
    """同じ入力の現行 Artifact があった。生成器を呼んでいない。"""

    artifact: ArtifactMetadata


@dataclass(frozen=True, slots=True)
class Submitted:
    """provider job 参照が台帳にある（今回 submit したか、既にあった）。await で待つ。"""

    reservation_id: str
    #: 今回の呼び出しで submit したか（再開なら False）
    newly_submitted: bool
    #: 台帳のラウンド
    round: int


SubmitOutcome = Reused | Submitted


@dataclass(frozen=True, slots=True)
class PaidOutput:
    """await の結果。``artifact`` があれば既に検証・保存済み（``data`` は空）。"""

    reservation: ProviderReservation
    data: bytes
    raw_output_key: str
    artifact: ArtifactMetadata | None = None


def raw_output_key(episode_id: str, reservation_id: str) -> str:
    return f"{PROVIDER_RAW_PREFIX}/{episode_id}/{reservation_id}/output.bin"


def raw_result_key(episode_id: str, reservation_id: str) -> str:
    return f"{PROVIDER_RAW_PREFIX}/{episode_id}/{reservation_id}/result.json"


def estimated_cost(value: float) -> Decimal:
    return Decimal(str(value)).quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)


class _FileDestination:
    """``MediaDestination`` のファイル実装（作業領域へストリーミング）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = path.open("wb")

    async def write(self, chunk: bytes) -> None:
        await asyncio.to_thread(self._handle.write, chunk)

    def close(self) -> None:
        self._handle.close()


class PaidJobRunner:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        workdir: WorkDirectory,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._workdir = workdir
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    # ------------------------------------------------------------------ submit

    async def submit(
        self, spec: PaidJobSpec, generator: AsyncJobGenerator, request: Any
    ) -> SubmitOutcome:
        reservation: ProviderReservation | None = None
        prepared = False
        for _ in range(_RESERVE_ATTEMPTS):
            async with self._session_factory() as session:
                # 再利用の唯一のゲート（ADR-0033）: DB行だけでなく実体（MinIO）も検証する。
                # 欠落・破損・版不一致は「現行が無い」のと同じに倒し、新ラウンドへ進む
                existing = await find_and_verify_current(
                    repo=ArtifactMetadataRepository(session),
                    store=self._store,
                    episode_id=spec.episode_id,
                    artifact_type=spec.artifact_type,
                    input_hash=spec.input_hash,
                    scene_id=spec.scene_id,
                    # 「現在有効な生成設定版」は呼び出し元（Activity）が spec に渡した値
                    # （fal の固定定数をここへ直接埋め込まない。fake/real どちらでも同じ形で効く）
                    current_generation_profile_id=spec.current_generation_profile_id,
                )
                if existing is not None:
                    return Reused(artifact=existing)

                reservations = ProviderReservationRepository(session)
                latest = await reservations.find_latest_for_input(
                    spec.episode_id, spec.provider, spec.scene_id, spec.input_hash
                )
                plan = _plan_round(latest)
                if isinstance(plan, Submitted):
                    return plan
                if isinstance(plan, ProviderReservation):
                    candidate: ProviderReservation | None = plan
                    ledger_round = plan.round
                else:
                    candidate = None
                    ledger_round = plan
                key = spec.key_for_round(ledger_round)
                # ここに来るのは「新しいラウンド」か
                # 「reserved + 未 dispatch」（呼んでいない証拠）だけ
                stale = [
                    row
                    for row in await reservations.find_unreconciled(
                        episode_id=spec.episode_id,
                        provider=spec.provider,
                        scene_id=spec.scene_id,
                    )
                    if row.idempotency_key != key
                ]
                if stale:
                    raise UnreconciledReservationError(
                        f"unreconciled reservation {stale[0].id} blocks a new "
                        f"{spec.provider.value} call for scene {spec.scene_id}"
                    )
                # 非課金の準備（例: 元画像のアップロード）は予約の**前**。
                # 失敗しても台帳に何も残らない。
                # 認可障害ゲート（ADR-0030）もここで初めて評価する: Reused / Submitted /
                # stale-unreconciled の早期returnより後なので、provider I/O が要らない
                # 済んだ工程の再開（sb1〜sb5 の再利用等）を抑止期間中でも止めない
                if not prepared:
                    await self._check_auth_outage_gate(spec.provider)
                    prepare = getattr(generator, "prepare", None)
                    if prepare is not None:
                        try:
                            request = await prepare(request)
                        except ProviderUnavailableError as exc:
                            await self._record_auth_incident(spec, exc)
                            raise
                        else:
                            await self._resolve_auth_incidents(spec.provider)
                    prepared = True
                if candidate is None:
                    try:
                        candidate = await reservations.reserve(
                            episode_id=spec.episode_id,
                            job_id=spec.job_id,
                            provider=spec.provider,
                            idempotency_key=key,
                            input_hash=spec.input_hash,
                            round=ledger_round,
                            scene_id=spec.scene_id,
                            estimated_cost_usd=estimated_cost(generator.estimate_cost_usd(request)),
                        )
                        await session.commit()
                    except IntegrityError:
                        # 並行する試行が同じラウンドを先に INSERT した。読み直して再開する
                        await session.rollback()
                        logger.info(
                            "ledger round %s for scene %s was reserved concurrently; re-reading",
                            ledger_round,
                            spec.scene_id,
                        )
                        continue
                reservation = candidate
                break
        if reservation is None:
            raise UnreconciledReservationError(
                f"could not settle a ledger round for scene {spec.scene_id} "
                f"after {_RESERVE_ATTEMPTS} concurrent attempts"
            )

        async with self._session_factory() as session:
            await ProviderReservationRepository(session).mark_dispatched(reservation.id)
            await session.commit()

        try:
            ref = await generator.submit(request)
        except NOT_ACCEPTED_SUBMIT_ERRORS as exc:
            await self._spend_conservatively(reservation.id, exc)
            raise
        except Exception as exc:
            logger.warning(
                "paid submit ambiguous; reservation left dispatched without ref "
                "reservation=%s episode=%s scene=%s error=%s",
                reservation.id,
                spec.episode_id,
                spec.scene_id,
                type(exc).__name__,
            )
            if isinstance(exc, ProviderSubmitAmbiguousError):
                raise
            raise ProviderSubmitAmbiguousError(
                f"paid submit outcome unknown: {type(exc).__name__}: {exc}"
            ) from exc

        # 参照が消えると回収できないので、commit 前にログにも残す（secret ではない）
        logger.info(
            "paid submit accepted reservation=%s episode=%s scene=%s ref=%s",
            reservation.id,
            spec.episode_id,
            spec.scene_id,
            ref,
        )
        async with self._session_factory() as session:
            await ProviderReservationRepository(session).record_provider_job_ref(
                reservation.id, ref
            )
            await session.commit()
        return Submitted(
            reservation_id=reservation.id, newly_submitted=True, round=reservation.round
        )

    # ------------------------------------------------------------------ await

    async def await_output(
        self,
        reservation_id: str,
        generator: AsyncJobGenerator,
        *,
        poll_interval_seconds: float,
        deadline_seconds: float | None = None,
        max_bytes: int | None = None,
        heartbeat: Callable[..., None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> PaidOutput:
        """provider job 参照に対して待ち、生の取得物を evidence にして返す。**再 submit しない**。

        cancel（``CancelledError``）はそのまま伝える。provider 側のジョブは cancel しない。
        """
        reservation = await self._load(reservation_id)
        raw_key = raw_output_key(reservation.episode_id, reservation.id)

        if reservation.outcome_artifact_id is not None:
            meta = await self._verified_outcome_artifact(reservation.outcome_artifact_id)
            if meta is not None:
                return PaidOutput(reservation, b"", reservation.raw_output_key or raw_key, meta)
            # 紐づいた Artifact が壊れている/欠落している（ADR-0033）。「紐づいて完了済み」を
            # 信じて素通りせず、下の evidence（生の取得物）から検証をやり直す経路へ落ちる。

        if reservation.status is ReservationStatus.SPENT:
            if reservation.raw_output_key is None:
                raise ProviderJobFailedError(
                    f"round consumed without output (reservation {reservation.id}): "
                    f"{reservation.error_summary or 'no evidence'}"
                )
            data = await self._store.get_bytes(reservation.raw_output_key)
            return PaidOutput(reservation, data, reservation.raw_output_key)
        if reservation.status is not ReservationStatus.RESERVED:
            raise ProviderJobFailedError(
                f"reservation {reservation.id} is {reservation.status.value}; nothing to await"
            )
        if reservation.provider_job_ref is None:
            raise UnreconciledReservationError(
                f"reservation {reservation.id} has no provider job ref to await"
            )

        # 取得物の保存と spent の commit の間で落ちていた: 保存済みの取得物が evidence
        if await self._store.exists(raw_key):
            async with self._keepalive(heartbeat, reservation.id, "evidence"):
                await self._mark_spent_with_evidence(reservation.id, raw_key)
                data = await self._store.get_bytes(raw_key)
                return await self._output_after_spent(reservation_id, data, raw_key)

        ref = ProviderJobRef(reservation.provider_job_ref)
        started = clock()
        polls = 0
        while True:
            polls += 1
            if heartbeat is not None:
                heartbeat({"reservation_id": reservation.id, "polls": polls})
            status = await generator.poll(ref)
            if isinstance(status, JobFailed):
                error: Exception = (
                    ProviderRejectedError(status.message)
                    if status.rejected
                    else ProviderJobFailedError(status.message)
                )
                await self._spend_conservatively(reservation.id, error)
                raise error
            if not isinstance(status, JobPending):
                break
            if deadline_seconds is not None and clock() - started >= deadline_seconds:
                raise ProviderPollDeadlineError(
                    f"provider job for reservation {reservation.id} still pending after "
                    f"{deadline_seconds}s; ref stays in the ledger for a later await"
                )
            await sleep(poll_interval_seconds)

        async with self._keepalive(heartbeat, reservation.id, "download"):
            data = await self._download(reservation, generator, ref, max_bytes=max_bytes)
        if heartbeat is not None:
            heartbeat({"reservation_id": reservation.id, "downloaded": len(data)})

        async with self._keepalive(heartbeat, reservation.id, "evidence"):
            await self._store_result_evidence(reservation, generator, ref)
            await self._store.put_bytes(raw_key, data, "application/octet-stream")
            await self._mark_spent_with_evidence(reservation.id, raw_key)
            return await self._output_after_spent(reservation_id, data, raw_key)

    async def record_output_rejected(self, reservation_id: str, exc: BaseException) -> None:
        """spent 済みの取得物が検証に落ちた。状態は変えず失敗を記録する。

        記録があると次の submit はこの予約を再開せず、新しいラウンドへ進む（ADR-0017 §3）。
        """
        async with self._session_factory() as session:
            await ProviderReservationRepository(session).record_failure(
                reservation_id,
                failure_class=classify_failure(exc),
                error_summary=f"{type(exc).__name__}: {exc}",
            )
            await session.commit()

    async def attach_artifact(self, session: AsyncSession, reservation_id: str, artifact_id: str):
        """呼び出し側の Artifact 記録と同じトランザクションで紐づける。"""
        return await ProviderReservationRepository(session).attach_artifact(
            reservation_id, artifact_id
        )

    # ------------------------------------------------------------------ internal

    async def _download(
        self,
        reservation: ProviderReservation,
        generator: AsyncJobGenerator,
        ref: ProviderJobRef,
        *,
        max_bytes: int | None,
    ) -> bytes:
        work_job_id = reservation.job_id or reservation.id
        work = self._workdir.create(reservation.episode_id, work_job_id)
        path = work.tmp / f"{reservation.id}.download"
        dest = _FileDestination(path)
        try:
            try:
                await generator.download(ref, dest)
            finally:
                dest.close()
            data = path.read_bytes()
            if max_bytes is not None and len(data) > max_bytes:
                raise MediaValidationError(
                    f"downloaded {len(data)} bytes, above the {max_bytes} byte cap"
                )
            if not data:
                raise ProviderJobFailedError("provider download was empty")
        except (MediaValidationError, ProviderJobFailedError, ProviderRejectedError) as exc:
            # 取得物そのものが使えない: 課金された前提でラウンドを確定する
            await self._spend_conservatively(reservation.id, exc)
            raise
        finally:
            path.unlink(missing_ok=True)
        self._workdir.cleanup(reservation.episode_id, work_job_id)
        return data

    async def _store_result_evidence(
        self, reservation: ProviderReservation, generator: AsyncJobGenerator, ref: ProviderJobRef
    ) -> None:
        describe = getattr(generator, "describe_result", None)
        if describe is None:
            return
        try:
            payload = await describe(ref)
            body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
            await self._store.put_text(raw_result_key(reservation.episode_id, reservation.id), body)
        except Exception as exc:  # evidence の補足情報。取得物本体が主証拠なので止めない
            logger.warning(
                "could not store provider result json reservation=%s: %s",
                reservation.id,
                type(exc).__name__,
            )

    async def _load(self, reservation_id: str) -> ProviderReservation:
        async with self._session_factory() as session:
            reservation = await ProviderReservationRepository(session).get(reservation_id)
        if reservation is None:
            raise UnreconciledReservationError(f"reservation {reservation_id} not found")
        return reservation

    async def _mark_spent_with_evidence(self, reservation_id: str, raw_key: str) -> None:
        """evidence で spent にする。**同じ evidence で既に spent なら冪等に受け入れる**。

        heartbeat 切れで並行した旧試行が先に commit していた場合に当たる。別の evidence や
        conservative で閉じていた場合は食い違いなので ``InvalidTransitionError`` のまま
        （needs_input）。
        """
        try:
            async with self._session_factory() as session:
                await ProviderReservationRepository(session).mark_spent(
                    reservation_id, raw_output_key=raw_key, reconciled_by="evidence"
                )
                await session.commit()
        except InvalidTransitionError:
            current = await self._load(reservation_id)
            if current.status is ReservationStatus.SPENT and current.raw_output_key == raw_key:
                logger.info(
                    "reservation already spent with the same evidence (concurrent attempt) "
                    "reservation=%s",
                    reservation_id,
                )
                return
            raise

    async def _output_after_spent(
        self, reservation_id: str, data: bytes, raw_key: str
    ) -> PaidOutput:
        reservation = await self._load(reservation_id)
        if reservation.outcome_artifact_id is not None:
            meta = await self._verified_outcome_artifact(reservation.outcome_artifact_id)
            if meta is not None:
                return PaidOutput(reservation, b"", raw_key, meta)
        return PaidOutput(reservation, data, raw_key)

    async def _verified_outcome_artifact(self, artifact_id: str) -> ArtifactMetadata | None:
        """紐づいた Artifact を実体まで検証してから返す（ADR-0033）。

        ``reservation.outcome_artifact_id`` が指す行は「この予約はもう完了している」という
        DB 上の主張でしかない。実体（MinIO）が欠落・破損していれば、それを「完了済み」として
        黙って返さない ── ``find_and_verify_current`` が予約の**前**でやっていることと同じ検査を、
        予約の**後**（await での再開）でも必ず通す。検証に落ちても行・object は削除・変更しない
        （呼び出し側が evidence から再検証する経路へ落ちるだけ）。
        """
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).get(artifact_id)
        if meta is None:
            return None
        result = await verify_artifact(self._store, meta)
        if result.verdict is not ArtifactVerdict.REUSABLE:
            return None
        return meta

    @contextlib.asynccontextmanager
    async def _keepalive(
        self, heartbeat: Callable[..., None] | None, reservation_id: str, phase: str
    ) -> AsyncIterator[None]:
        """単発の長い await の間、背景タスクで heartbeat を送る。

        Temporal の activity context は contextvar なので、Activity 内で作ったタスクから送れる。
        """
        if heartbeat is None:
            yield
            return
        beat = heartbeat
        interval = self._heartbeat_interval_seconds

        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval)
                beat({"reservation_id": reservation_id, "phase": phase})

        task = asyncio.create_task(_loop())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ---------------------------------------------------- provider 認可障害の抑止（ADR-0030）

    async def _check_auth_outage_gate(self, provider: ProviderCall) -> None:
        """同じ provider の未解決 incident が閾値を超えていれば、予約を作らずに止める。"""
        since = datetime.now(UTC) - timedelta(minutes=AUTH_INCIDENT_WINDOW_MINUTES)
        async with self._session_factory() as session:
            count = await ProviderAuthIncidentRepository(session).count_unresolved_within_window(
                provider, since=since
            )
        if count >= AUTH_INCIDENT_SUPPRESSION_THRESHOLD:
            raise ProviderCredentialSuspectedOutageError(
                f"{provider.value}: {count} unresolved auth incidents in the last "
                f"{AUTH_INCIDENT_WINDOW_MINUTES} minutes; suppressing new submits until "
                "a call succeeds or the incidents are resolved"
            )

    async def _record_auth_incident(self, spec: PaidJobSpec, exc: ProviderUnavailableError) -> None:
        async with self._session_factory() as session:
            await ProviderAuthIncidentRepository(session).record(
                provider=spec.provider,
                http_status=getattr(exc, "http_status", None),
                episode_id=spec.episode_id,
                now=datetime.now(UTC),
            )
            await session.commit()

    async def _resolve_auth_incidents(self, provider: ProviderCall) -> None:
        async with self._session_factory() as session:
            resolved = await ProviderAuthIncidentRepository(session).resolve_open_for_provider(
                provider, now=datetime.now(UTC)
            )
            await session.commit()
        if resolved:
            logger.info(
                "resolved %s open auth incident(s) for provider=%s after a successful call",
                resolved,
                provider.value,
            )

    async def _spend_conservatively(self, reservation_id: str, exc: BaseException) -> None:
        async with self._session_factory() as session:
            await ProviderReservationRepository(session).mark_spent(
                reservation_id,
                raw_output_key=None,
                reconciled_by="conservative",
                failure_class=classify_failure(exc),
                error_summary=f"{type(exc).__name__}: {exc}",
            )
            await session.commit()


def _plan_round(latest: ProviderReservation | None) -> Submitted | ProviderReservation | int:
    """同じ入力の最新の予約から次の動作を決める（ADR-0017 §3）。

    - ``Submitted``: その予約を await で再開する（再 submit しない）
    - ``ProviderReservation``: ``reserved`` + 未 dispatch。この予約のまま dispatch へ進む
    - ``int``: 新しい台帳ラウンドの番号
    """
    if latest is None:
        return 1
    if latest.status is ReservationStatus.RESERVED:
        if latest.provider_job_ref is not None:
            return Submitted(reservation_id=latest.id, newly_submitted=False, round=latest.round)
        if latest.dispatched_at is not None:
            raise UnreconciledReservationError(
                f"reservation {latest.id} was dispatched without a provider job ref"
            )
        return latest
    if latest.outcome_artifact_id is not None:
        # 紐づいた Artifact は await が返す
        return Submitted(reservation_id=latest.id, newly_submitted=False, round=latest.round)
    if (
        latest.status is ReservationStatus.SPENT
        and latest.raw_output_key is not None
        and latest.failure_class is None
    ):
        # spent と Artifact 記録の間で落ちた: 保存済みの取得物から検証を再開する
        return Submitted(reservation_id=latest.id, newly_submitted=False, round=latest.round)
    # 取得物なしで spent（ジョブ失敗・受理されず）/ 検証に落ちた evidence / abandoned
    return latest.round + 1


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "NOT_ACCEPTED_SUBMIT_ERRORS",
    "PROVIDER_RAW_PREFIX",
    "AsyncJobGenerator",
    "PaidJobRunner",
    "PaidJobSpec",
    "PaidOutput",
    "Reused",
    "SubmitOutcome",
    "Submitted",
    "estimated_cost",
    "raw_output_key",
    "raw_result_key",
]
