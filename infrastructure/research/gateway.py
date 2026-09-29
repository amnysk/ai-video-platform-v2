"""Research Gateway（ADR-0037 §5 / §6 / §8）: 依頼の受け付け・鮮度キャッシュ・予算の門・再開。

独立したプロセスではない（API と Worker が同じ関数を呼ぶ）。Workflow の開始は Worker の段（B3）で
足す。ここは**DB に依頼を確定させるまで**を持つ。

``submit`` の手順（順序が意味を持つ）:

1. 契約から依頼を作る（``submit_to_spec``。依頼が上限を指定しなければ設定値の金額・quota の上限を
   **依頼に凍結**する）
2. ``request_hash``（``domain/research/identity.py``。意味の同一性）
3. 同じ ``idempotency_key`` の依頼があればそれを返す（意味が違えば
   ``ResearchIdempotencyConflictError``。保存済みの行は変えない）
4. 鮮度キャッシュ: 同じ hash で**完了済み・鮮度の窓の中**（Trend は ``trend_fresh_hours``、
   Evidence は ``evidence_reverify_days``）の依頼があり、その**現行の成果物の本体を読み戻して
   sha256 が記録と一致したら**それを返す（新しく保存も実行もしない）。一致しない・本体が無い・
   結果が指す成果物と食い違う候補は再利用しない（base INV-31 の考え方）
5. ``create_or_get``（``request_id = uuid5(idempotency_key)``）
6. 予算の門（``domain/research/admission.py::admission_block``）: Provider が未設定、
   または実 Provider で金額・quota の上限のどちらかが無ければ、外部を呼ばずに ``blocked``
   （理由コードを記録する）

``resume``（``blocked → queued``）: 同じ門を通る。凍結した上限が足りない依頼・Provider が未設定の
ままの依頼は再開しない（再開しても同じ理由でまた止まるだけ）。DB の遷移（compare-and-set）が門で、
同時に 2 回呼んでも 1 つだけが再開する。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.research import (
    EVIDENCE_REVERIFY_DAYS,
    TREND_FRESH_HOURS,
    EvidenceResearchSubmit,
    ResearchCoverage,
    ResearchKind,
    ResearchLimits,
    ResearchResult,
    ResearchStatus,
    TrendResearchSubmit,
    submit_to_spec,
)
from domain.errors import InvalidTransitionError
from domain.research.admission import StopReason, admission_block
from domain.research.entities import ResearchRequest
from domain.research.errors import ResearchIdempotencyConflictError
from domain.research.identity import request_hash
from domain.research.keys import research_artifact_object_key
from infrastructure.config import Settings
from infrastructure.db.research_repositories import (
    ResearchArtifactRepository,
    ResearchRequestRepository,
)
from infrastructure.research.registry import (
    provider_config_version,
    provider_is_configured,
    provider_is_real,
)
from infrastructure.storage.artifact_store import ArtifactStore, readback_sha256

__all__ = [
    "GatewayConfig",
    "ResearchGateway",
    "ResumeResult",
    "SubmitResult",
    "block_request",
]

logger = logging.getLogger(__name__)

#: 再開しなかった理由（``blocked`` 以外の依頼）
NOT_BLOCKED = "not_blocked"


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    provider_mode: str
    provider_is_real: bool
    provider_configured: bool
    trend_fresh_hours: int = TREND_FRESH_HOURS
    evidence_reverify_days: int = EVIDENCE_REVERIFY_DAYS
    #: 依頼が上限を指定しなかったときに凍結する金額・quota の上限（設定値）
    default_max_cost_usd: Decimal | None = None
    default_max_youtube_units: int | None = None

    @property
    def provider_config_version(self) -> str:
        return provider_config_version(self.provider_mode)

    @classmethod
    def from_settings(cls, settings: Settings) -> GatewayConfig:
        mode = settings.research_provider
        return cls(
            provider_mode=mode,
            provider_is_real=provider_is_real(mode),
            provider_configured=provider_is_configured(mode),
            trend_fresh_hours=settings.trend_fresh_hours,
            evidence_reverify_days=settings.evidence_reverify_days,
            default_max_cost_usd=settings.research_max_cost_usd,
            default_max_youtube_units=settings.research_max_youtube_units,
        )

    def block_reason(self, limits: ResearchLimits) -> StopReason | None:
        return admission_block(
            limits,
            provider_configured=self.provider_configured,
            provider_is_real=self.provider_is_real,
        )


@dataclass(frozen=True, slots=True)
class SubmitResult:
    request: ResearchRequest
    #: 鮮度内の完了済み依頼を（検証の上で）再利用した。新しく保存も実行もしていない
    reused: bool = False


@dataclass(frozen=True, slots=True)
class ResumeResult:
    request: ResearchRequest
    #: この呼び出しが ``blocked → queued`` を行った
    resumed: bool
    #: ``resumed`` が偽の理由（``not_blocked`` / 門の理由コード）
    reason: str | None = None


async def block_request(
    session: AsyncSession, request: ResearchRequest, reason: StopReason
) -> ResearchRequest:
    """外部を呼ばずに ``blocked`` にする（``queued → running → blocked``）。commit しない。

    開始した記録（``started_at``）は残す（状態表は ``blocked`` へ ``running`` からしか行かない）。
    """
    requests = ResearchRequestRepository(session)
    await requests.mark_running(request.id)
    return await requests.finish(
        request.id,
        ResearchStatus.BLOCKED,
        result=ResearchResult(
            request_id=request.id,
            execution_status=ResearchStatus.BLOCKED,
            coverage=ResearchCoverage(items_requested=0, items_covered=0),
            warnings=(reason.detail[:300],),
            stop_code=reason.code,
        ),
        blocked_reason=reason.as_blocked_reason(),
    )


class ResearchGateway:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        config: GatewayConfig,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._config = config
        self._clock = clock

    # ------------------------------------------------------------------ 受け付け

    async def submit(self, submit: TrendResearchSubmit | EvidenceResearchSubmit) -> SubmitResult:
        spec = submit_to_spec(submit)
        if "limits" not in submit.model_fields_set:
            spec = spec.model_copy(update={"limits": self._default_limits(spec.limits)})
        version = self._config.provider_config_version
        digest = request_hash(spec, provider_config_version=version)

        async with self._session_factory() as session:
            requests = ResearchRequestRepository(session)
            existing = await requests.get_by_idempotency_key(submit.idempotency_key)
            if existing is not None:
                if existing.request_hash != digest:
                    raise ResearchIdempotencyConflictError(
                        f"idempotency key {submit.idempotency_key!r} was already used for a "
                        "different request (request_hash differs)"
                    )
                return SubmitResult(existing)

            candidate = await requests.find_reusable(digest, self._reuse_horizon(spec.kind))
            if candidate is not None and await self._verified(session, candidate):
                return SubmitResult(candidate, reused=True)

            created = await requests.create_or_get(
                idempotency_key=submit.idempotency_key,
                spec=spec,
                provider_config_version=version,
            )
            await session.commit()
            reason = self._config.block_reason(spec.limits)
            if reason is not None and created.status is ResearchStatus.QUEUED:
                created = await block_request(session, created, reason)
                await session.commit()
        return SubmitResult(created)

    def _default_limits(self, limits: ResearchLimits) -> ResearchLimits:
        data = limits.model_dump()
        data["max_cost_usd"] = self._config.default_max_cost_usd
        data["max_youtube_units"] = self._config.default_max_youtube_units
        return ResearchLimits.model_validate(data)

    def _reuse_horizon(self, kind: ResearchKind) -> datetime:
        now = self._clock()
        if kind is ResearchKind.TREND:
            return now - timedelta(hours=self._config.trend_fresh_hours)
        return now - timedelta(days=self._config.evidence_reverify_days)

    async def _verified(self, session: AsyncSession, request: ResearchRequest) -> bool:
        """再利用の前に、結果が指す現行の成果物の本体を読み戻して sha256 を照合する。

        結果（``result_summary``）が指す成果物と現行の行が食い違う・キーが research のキーでない・
        本体が無い・sha256 が違う、のどれでも再利用しない（新しい依頼として実行する）。
        """
        if not request.result_summary:
            return False
        result = ResearchResult.model_validate(request.result_summary)
        current = {
            (a.id, a.artifact_type, a.sha256): a
            for a in await ResearchArtifactRepository(session).list_current(request.id)
        }
        if not result.artifact_refs:
            return False
        for ref in result.artifact_refs:
            record = current.get((ref.artifact_id, ref.artifact_type, ref.sha256))
            if record is None:
                logger.warning("research reuse rejected: result references a non-current artifact")
                return False
            expected_key = research_artifact_object_key(
                request.id, record.artifact_type, record.sha256
            )
            if record.object_key != expected_key:
                logger.warning("research reuse rejected: artifact key is not the research key")
                return False
            try:
                actual = await readback_sha256(self._store, record.object_key)
            except KeyError:
                logger.warning("research reuse rejected: stored artifact is missing")
                return False
            if actual != record.sha256:
                logger.warning("research reuse rejected: stored artifact sha256 mismatch")
                return False
        return True

    # ------------------------------------------------------------------ 再開

    async def resume(self, request_id: str) -> ResumeResult | None:
        """``blocked`` の依頼を ``queued`` に戻す。未知の依頼は ``None``。Workflow の開始は B3。"""
        async with self._session_factory() as session:
            requests = ResearchRequestRepository(session)
            current = await requests.get(request_id)
            if current is None:
                return None
            if current.status is not ResearchStatus.BLOCKED:
                return ResumeResult(current, resumed=False, reason=NOT_BLOCKED)
            reason = self._config.block_reason(ResearchLimits.model_validate(current.limits))
            if reason is not None:
                return ResumeResult(current, resumed=False, reason=reason.code.value)
            try:
                queued = await requests.resume(request_id)
                await session.commit()
            except InvalidTransitionError:
                await session.rollback()  # 同時の再開が先に遷移した
                latest = await requests.get(request_id)
                return ResumeResult(latest or current, resumed=False, reason=NOT_BLOCKED)
        return ResumeResult(queued, resumed=True)

    # ------------------------------------------------------------------ 参照

    async def get(self, request_id: str) -> ResearchRequest | None:
        async with self._session_factory() as session:
            return await ResearchRequestRepository(session).get(request_id)
