"""Research の実行器（Temporal 非依存。ADR-0037 §4 / §5 / §8）。

Worker（B3）の Activity はこのクラスの薄いラッパになる。ここは DB・ArtifactStore・Provider の Port
だけで動くので、Fake とインメモリ / SQLite でクラッシュ境界を再現して検査できる。種別（Trend /
Evidence）の**判断**は ``ResearchHandler``（``domain/research/handlers.py``）の背後にあり、ここは
骨格（門・予約・呼び出し・生データ・成果物・状態）だけを持つ。

本番の課金コード（``PaidJobRunner`` / ``infrastructure/production``）と本番のリポジトリ
（``repositories.py``）は使わない（INV-37）。台帳は ``research_calls``（INV-36）。

外部呼び出し 1 件の書き込み順序（ADR-0013 / INV-15 と同じ規律。入れ替えない）::

    reserve → commit → mark_dispatched → commit → 呼び出し
      → 生データを research/{request_id}/raw/ へ保存 → mark_spent → commit

呼び出しキーは ``research:{request_id}:{call}:{input_hash[:16]}``（2 回目以降は ``:r{n}``）。
**Temporal の attempt を含めない**ので、再実行は同じキー・同じ行に戻る。
同じ入力の最新の行で分岐する:

======================================  ==========================================================
同じ入力の最新の行                      動作
======================================  ==========================================================
無い                                    新しく予約する（1 回目）
spent + 生データあり                    呼ばずに保存済みの生データを使う
spent + 生データ無し（恒久的な失敗）    送り直さない（同じ入力では同じ結果）。穴として記録する
spent + 生データ無し（一時障害・停止）  前回は戻ってきた上で失敗した。新しい番号で予約する
/ abandoned                             （retry も合計で枠を数える。INV-36）
reserved + dispatched + 生データあり    呼んだ後・spent の前に落ちた。``spent`` にして生データを使う
reserved + dispatched + 生データ無し    成否不明。**送り直さない・解放しない**。依頼は ``blocked``
reserved + dispatch 前                  呼んでいない証拠。その行で dispatch から続ける
======================================  ==========================================================

呼び出しの失敗の扱い（ADR-0037 §6 / §8）:

- 一時障害（5xx・通信断・timeout）: ``spent`` に確定してから ``ResearchSourceUnavailableError``
  （retryable）。予約を ``reserved`` のまま残さない。Temporal の retry は新しい番号を取る
- rate limit・quota 枯渇・認証拒否・Provider 未設定: ``spent`` に確定し、その種別の以後の呼び出しを
  止める。使える結果があれば ``partial``、無ければ ``blocked``（理由コードを記録）
- 上限（件数・金額・quota。INV-36）に達した: 予約の前に止まる（行を作らない）。以後の同じ種別の
  呼び出しを止め、``partial``（使える結果が無ければ ``blocked``）
- 恒久的な失敗（入力の拒否・取得できない URL）: その 1 件の穴として記録し、続ける
- 分類できない例外: 予約は dispatch 済みのまま（成否不明）で伝える。再実行は送り直さない

成果物: 正準 JSON を ``research/{request_id}/{type}/{sha256}.json`` に ``put_json`` → 読み戻して
sha256 を照合 → ``research_artifacts`` に記録 → 依頼を ``completed`` / ``partial`` に進める（記録と
状態は同じ commit）。照合が合わなければ記録しない（``ResearchArtifactReadbackError``。retryable）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.research import (
    ResearchArtifactRef,
    ResearchCall,
    ResearchCallStatus,
    ResearchCoverage,
    ResearchKind,
    ResearchLimits,
    ResearchResult,
    ResearchStatus,
    ResearchStopCode,
    ResearchUsage,
    parse_research_spec,
)
from contracts.states import FailureClass
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.errors import NeedsInputError, PermanentError, RetryableError, TransientError
from domain.research.admission import StopReason, admission_block
from domain.research.entities import ResearchRequest
from domain.research.errors import (
    ResearchAmbiguousCallError,
    ResearchArtifactReadbackError,
    ResearchBudgetExceededError,
    ResearchInputInvalidError,
    ResearchOutputInvalidError,
    ResearchSourceUnavailableError,
    research_failure_class_from_type_name,
)
from domain.research.handlers import (
    FetchedSource,
    FetchTarget,
    HandlerOutput,
    ResearchHandler,
    ResearchSpec,
    SearchRound,
    SearchStep,
    SynthesisContext,
    dedupe_fetch_targets,
    plan_within_ceiling,
)
from domain.research.keys import research_artifact_object_key
from domain.research.ports import FetchedContent, SearchQuery, SearchResults
from domain.research.status import RESEARCH_TERMINAL_STATUSES
from domain.research.urls import normalize_url
from infrastructure.db.research_repositories import (
    ResearchArtifactRepository,
    ResearchCallRepository,
    ResearchRequestRepository,
)
from infrastructure.research.errors import (
    ProviderAuthRejected,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ResearchProviderNotConfigured,
    fetch_failure_to_domain_error,
    to_domain_error,
)
from infrastructure.research.raw_store import FETCH_CODEC, SEARCH_CODEC, RawCodec, ResearchRawStore
from infrastructure.research.registry import CostModel, ResearchProviders
from infrastructure.storage.artifact_store import ArtifactStore, readback_sha256
from infrastructure.youtube.errors import (
    YouTubeAuthError,
    YouTubeQuotaError,
    YouTubeRateLimitError,
)

__all__ = [
    "ResearchExecution",
    "ResearchExecutor",
    "call_idempotency_key",
    "query_hash",
    "url_hash",
]

logger = logging.getLogger(__name__)

_MAX_TEXT = 300
_MAX_WARNINGS = 50
#: 台帳の ``provider`` 列（診断用ラベル）の長さ
_PROVIDER_LABEL_MAX = 32
_FINISHED = RESEARCH_TERMINAL_STATUSES | {ResearchStatus.BLOCKED}

#: 生データの無い ``spent`` 行の ``error_summary`` の接頭辞。再実行の分岐に使う
#: （恒久的な失敗は送り直さない。一時障害・停止は新しい番号で送り直してよい）
_PERMANENT = "permanent:"
_TRANSIENT = "transient:"
_STOPPED = "stop:"

#: 止めた理由のうち、使える結果が無いときに ``failed``（``blocked`` ではない）にするもの
_FAIL_WHEN_EMPTY = frozenset({ResearchStopCode.DEADLINE_EXCEEDED})


# ------------------------------------------------------------------ 呼び出しの同一性


def query_hash(query: SearchQuery) -> str:
    """検索の入力 hash（同じ検索は同じ hash。時刻の揺れを含めない）。"""
    payload = {
        "text": query.text,
        "kind": query.kind,
        "max_results": query.max_results,
        "region_code": query.region_code,
        "language": query.language,
        "published_after": query.published_after.isoformat() if query.published_after else None,
        "published_before": query.published_before.isoformat() if query.published_before else None,
    }
    return sha256_hex(canonical_json_bytes(payload))


def url_hash(url: str) -> str:
    """取得の入力 hash（正規化 URL。同じ資料を 2 回取得しない）。"""
    return sha256_hex(normalize_url(url).encode("utf-8"))


def call_idempotency_key(
    request_id: str, call: ResearchCall, input_hash: str, round_no: int
) -> str:
    """呼び出しの冪等キー。**Temporal の attempt を含めない**（再実行が枠を消費しない）。"""
    if round_no < 1:
        raise ValueError("round_no must be >= 1")
    base = f"research:{request_id}:{call.value}:{input_hash[:16]}"
    return base if round_no == 1 else f"{base}:r{round_no}"


# ------------------------------------------------------------------ 結果


@dataclass(frozen=True, slots=True)
class ResearchExecution:
    """実行の結果（Worker が workflow へ返す形の元）。状態の正本は DB。"""

    request_id: str
    status: ResearchStatus
    artifact_refs: tuple[ResearchArtifactRef, ...] = ()
    #: 止めた・縮めた理由（``ResearchStopCode`` の値）。``completed`` なら ``None``
    stop_code: str | None = None
    usage: ResearchUsage = field(default_factory=ResearchUsage)


@dataclass(frozen=True, slots=True)
class _Loaded:
    request: ResearchRequest
    spec: ResearchSpec
    limits: ResearchLimits


@dataclass(frozen=True, slots=True)
class _Done[T]:
    """呼んで結果を得た（または保存済みの結果を読んだ）。"""

    value: T


@dataclass(frozen=True, slots=True)
class _Stopped:
    """この種別の以後の呼び出しを止める。"""

    reason: StopReason


@dataclass(frozen=True, slots=True)
class _Failed:
    """この 1 件は恒久的に失敗した（穴として記録して続ける）。"""

    code: str


type _CallOutcome[T] = _Done[T] | _Stopped | _Failed


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def _utc(value: datetime) -> datetime:
    """SQLite（unit テスト）は tz を落とす。PostgreSQL の timestamptz は無変更。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _stop(code: ResearchStopCode, detail: str) -> StopReason:
    return StopReason(code, _clip(detail))


def _classify_exception(exc: Exception) -> StopReason | str | None:
    """呼び出しが投げた例外の扱い。

    ``StopReason``: 以後を止める。``str``: 恒久的な失敗（理由コード）。``None``: 一時障害（retry）。
    分類できない例外は ``to_domain_error`` がそのまま送出する（予約は成否不明のまま残る）。
    """
    if isinstance(exc, ProviderRateLimited | YouTubeRateLimitError):
        return _stop(ResearchStopCode.RATE_LIMITED, f"provider rate limited: {type(exc).__name__}")
    if isinstance(exc, ProviderQuotaExhausted | YouTubeQuotaError):
        return _stop(ResearchStopCode.QUOTA_EXHAUSTED, f"provider quota: {type(exc).__name__}")
    if isinstance(exc, ProviderAuthRejected | YouTubeAuthError):
        return _stop(ResearchStopCode.PROVIDER_AUTH, f"provider auth: {type(exc).__name__}")
    if isinstance(exc, ResearchProviderNotConfigured):
        return _stop(ResearchStopCode.PROVIDER_NOT_CONFIGURED, "provider is not configured")
    mapped = to_domain_error(exc)
    if isinstance(mapped, RetryableError | TransientError):
        return None
    if isinstance(mapped, PermanentError):
        return "rejected"
    if isinstance(mapped, NeedsInputError):
        return _stop(ResearchStopCode.PROVIDER_AUTH, f"provider needs input: {type(exc).__name__}")
    return None  # pragma: no cover - DomainError の直下はこの 4 つ


def _classify_fetch(content: FetchedContent) -> StopReason | str | None:
    """値で返る取得失敗の扱い。成功（切り詰めを含む）は ``""``。"""
    if content.fetch_status != "failed":
        return ""
    if content.error == "http_4xx" and content.status_code == 429:
        return _stop(ResearchStopCode.RATE_LIMITED, "fetch rate limited (429)")
    failure = fetch_failure_to_domain_error(content)
    if isinstance(failure, RetryableError):
        return None
    return content.error or "failed"


class ResearchExecutor:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        bucket: str,
        providers: ResearchProviders,
        handlers: Mapping[ResearchKind, ResearchHandler],
        cost_model: CostModel | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._bucket = bucket
        self._providers = providers
        self._handlers = dict(handlers)
        self._cost = cost_model or CostModel()
        self._clock = clock
        self._raw = ResearchRawStore(store)

    # ------------------------------------------------------------------ 入口

    async def execute(self, request_id: str) -> ResearchExecution:
        """依頼を 1 回実行する。何度呼んでも同じ呼び出しを送り直さない（冪等）。

        一時障害は ``ResearchSourceUnavailableError`` を、成果物の読み戻し不一致は
        ``ResearchArtifactReadbackError`` を送出する（どちらも retryable。Worker が retry する）。
        """
        loaded = await self._load(request_id)
        if loaded.request.status in _FINISHED:
            return self._outcome_of(loaded.request)

        reason = admission_block(
            loaded.limits,
            provider_configured=self._providers.configured,
            provider_is_real=self._providers.is_real,
        )
        if reason is not None:
            return await self._finish_without_artifact(loaded, ResearchStatus.BLOCKED, reason)
        handler = self._handlers.get(loaded.request.kind)
        if handler is None:
            return await self._finish_without_artifact(
                loaded,
                ResearchStatus.BLOCKED,
                _stop(
                    ResearchStopCode.HANDLER_NOT_AVAILABLE,
                    f"no research handler is registered for {loaded.request.kind.value}",
                ),
            )

        async with self._session_factory() as session:
            running = await ResearchRequestRepository(session).mark_running(request_id)
            await session.commit()
        started = _utc(running.started_at or self._clock())
        return await self._run(loaded, handler, started)

    async def record_failure(
        self, request_id: str, *, error_type: str | None, summary: str
    ) -> ResearchExecution:
        """retry を使い切った・想定外の失敗を記録する（Worker が workflow の外から呼ぶ）。

        分類は型名で行う（``research_failure_class_from_type_name``。research の型も引ける）。
        ``needs_input`` は ``blocked``（人が直して再開できる）、それ以外は ``failed``。
        """
        loaded = await self._load(request_id)
        failure_class = research_failure_class_from_type_name(error_type)
        status = (
            ResearchStatus.BLOCKED
            if failure_class is FailureClass.NEEDS_INPUT
            else ResearchStatus.FAILED
        )
        detail = f"{error_type or 'unknown'}: {summary}"
        return await self._finish_without_artifact(
            loaded, status, _stop(ResearchStopCode.EXECUTION_FAILED, detail)
        )

    # ------------------------------------------------------------------ 読み込み

    async def _load(self, request_id: str) -> _Loaded:
        async with self._session_factory() as session:
            request = await ResearchRequestRepository(session).get(request_id)
        if request is None:
            raise ResearchInputInvalidError(f"research request not found: {request_id}")
        try:
            spec = parse_research_spec(request.payload)
            limits = ResearchLimits.model_validate(request.limits)
        except ValidationError as exc:
            raise ResearchInputInvalidError(
                f"stored research request {request_id} violates its contract"
            ) from exc
        return _Loaded(request=request, spec=spec, limits=limits)

    # ------------------------------------------------------------------ 本体

    def _past_deadline(self, started: datetime, limits: ResearchLimits) -> bool:
        return self._clock() > started + timedelta(seconds=limits.deadline_seconds)

    async def _run(
        self, loaded: _Loaded, handler: ResearchHandler, started: datetime
    ) -> ResearchExecution:
        spec, limits = loaded.spec, loaded.limits
        try:
            steps, searches_skipped = plan_within_ceiling(
                handler.plan_searches(spec, max_searches=limits.max_searches),
                max_searches=limits.max_searches,
            )
        except ValueError as exc:
            raise ResearchOutputInvalidError(f"invalid search plan: {exc}") from exc
        skipped_searches = list(searches_skipped)
        plan_cut = bool(searches_skipped)

        rounds: list[SearchRound] = []
        search_stop: StopReason | None = None
        for step in steps:
            if search_stop is None and self._past_deadline(started, limits):
                search_stop = _stop(ResearchStopCode.DEADLINE_EXCEEDED, "research deadline passed")
            if search_stop is not None:
                skipped_searches.append(step.step_id)
                continue
            outcome = await self._search(loaded, step)
            if isinstance(outcome, _Done):
                rounds.append(SearchRound(step=step, results=outcome.value))
            elif isinstance(outcome, _Failed):
                rounds.append(SearchRound(step=step, results=None, error=outcome.code))
            else:
                search_stop = outcome.reason
                rounds.append(SearchRound(step=step, results=None, error=outcome.reason.code.value))

        if search_stop is not None and search_stop.code is ResearchStopCode.AMBIGUOUS_CALL:
            return await self._finish_without_artifact(loaded, ResearchStatus.BLOCKED, search_stop)
        usable = [r for r in rounds if r.results is not None]
        if steps and not usable:
            if search_stop is None:
                empty = _stop(ResearchStopCode.NO_USABLE_RESULTS, "no search produced a result")
                return await self._finish_without_artifact(loaded, ResearchStatus.FAILED, empty)
            status = (
                ResearchStatus.FAILED
                if search_stop.code in _FAIL_WHEN_EMPTY
                else ResearchStatus.BLOCKED
            )
            return await self._finish_without_artifact(loaded, status, search_stop)

        sources, skipped_fetches, failures, fetch_stop = await self._fetch_all(
            loaded, handler, rounds, started
        )
        if fetch_stop is not None and fetch_stop.code is ResearchStopCode.AMBIGUOUS_CALL:
            return await self._finish_without_artifact(loaded, ResearchStatus.BLOCKED, fetch_stop)
        failures = [f"search:{r.step.step_id}:{r.error}" for r in rounds if r.error] + failures

        ctx = SynthesisContext(
            request_id=loaded.request.id,
            as_of=spec.as_of,
            searches_skipped=tuple(skipped_searches),
            fetches_skipped=tuple(skipped_fetches),
            failures=tuple(failures),
        )
        output = handler.synthesize(spec, ctx, rounds, sources)
        stop = search_stop or fetch_stop
        if stop is None and plan_cut:
            stop = _stop(
                ResearchStopCode.CALL_BUDGET_EXHAUSTED,
                "the plan had more searches than max_searches",
            )
        degraded = bool(skipped_searches or skipped_fetches or failures or stop)
        status = (
            ResearchStatus.PARTIAL if degraded or not output.complete else ResearchStatus.COMPLETED
        )
        warnings = self._warnings(output, skipped_searches, skipped_fetches, failures, stop)
        return await self._finalize(loaded, handler, output, status, stop, warnings)

    async def _fetch_all(
        self,
        loaded: _Loaded,
        handler: ResearchHandler,
        rounds: Sequence[SearchRound],
        started: datetime,
    ) -> tuple[list[FetchedSource], list[str], list[str], StopReason | None]:
        """(取得結果, 実行しなかった取得, 穴の理由コード, 止めた理由)。"""
        limits = loaded.limits
        chosen = handler.select_fetches(
            loaded.spec, rounds, remaining=limits.max_fetches, already_fetched=()
        )
        targets = dedupe_fetch_targets(chosen, already_fetched=(), remaining=limits.max_fetches)
        sources: list[FetchedSource] = []
        skipped: list[str] = []
        failures: list[str] = []
        stop: StopReason | None = None
        for index, target in enumerate(targets, start=1):
            label = f"f{index:02d}"
            if stop is None and self._past_deadline(started, limits):
                stop = _stop(ResearchStopCode.DEADLINE_EXCEEDED, "research deadline passed")
            if stop is not None:
                skipped.append(label)
                continue
            outcome = await self._fetch(loaded, target)
            if isinstance(outcome, _Done):
                content = outcome.value
                if content.fetch_status == "failed":
                    failures.append(f"fetch:{label}:{content.error or 'failed'}")
                sources.append(FetchedSource(target=target, content=content))
            elif isinstance(outcome, _Failed):
                failures.append(f"fetch:{label}:{outcome.code}")
            else:
                stop = outcome.reason
                skipped.append(label)
        return sources, skipped, failures, stop

    async def _search(self, loaded: _Loaded, step: SearchStep) -> _CallOutcome[SearchResults]:
        provider = self._providers.search
        if provider is None:  # admission が先に止めるので通常は来ない
            return _Stopped(_stop(ResearchStopCode.PROVIDER_NOT_CONFIGURED, "no search provider"))
        query = step.query
        return await self._run_call(
            loaded,
            call=ResearchCall.SEARCH,
            input_hash=query_hash(query),
            provider_label=str(getattr(provider, "name", self._providers.mode)),
            quota_units=self._cost.quota_units_for(query.kind),
            codec=SEARCH_CODEC,
            invoke=lambda: provider.search(query),
            classify_value=lambda _results: "",
        )

    async def _fetch(self, loaded: _Loaded, target: FetchTarget) -> _CallOutcome[FetchedContent]:
        fetcher = self._providers.fetcher
        if fetcher is None:
            return _Stopped(_stop(ResearchStopCode.PROVIDER_NOT_CONFIGURED, "no content fetcher"))
        url = target.hit.url
        return await self._run_call(
            loaded,
            call=ResearchCall.FETCH,
            input_hash=url_hash(url),
            provider_label=self._providers.mode,
            quota_units=None,
            codec=FETCH_CODEC,
            invoke=lambda: fetcher.fetch(url),
            classify_value=_classify_fetch,
        )

    # ------------------------------------------------------------------ 外部呼び出し 1 件

    async def _run_call[T](
        self,
        loaded: _Loaded,
        *,
        call: ResearchCall,
        input_hash: str,
        provider_label: str,
        quota_units: int | None,
        codec: RawCodec[T],
        invoke: Callable[[], Awaitable[T]],
        classify_value: Callable[[T], StopReason | str | None],
    ) -> _CallOutcome[T]:
        """予約 → dispatch → 呼び出し → 生データ → spent。再実行の分岐表は module docstring。"""
        request_id = loaded.request.id
        async with self._session_factory() as session:
            calls = ResearchCallRepository(session)
            mine = sorted(
                (
                    c
                    for c in await calls.list_for_request(request_id)
                    if c.call is call and c.input_hash == input_hash
                ),
                key=lambda c: c.call_seq,
            )
            record = None
            if mine:
                latest = mine[-1]
                saved = await self._raw.find(codec, request_id, latest.id)
                if latest.status is ResearchCallStatus.SPENT and saved is not None:
                    return _Done(saved)
                if latest.status is ResearchCallStatus.SPENT and (
                    latest.error_summary or ""
                ).startswith(_PERMANENT):
                    return _Failed("rejected")  # 同じ入力では同じ結果。送り直さない
                if latest.status is ResearchCallStatus.RESERVED:
                    if saved is not None:  # 呼んで保存した後・spent の前に落ちた
                        await calls.mark_spent(latest.id)
                        await session.commit()
                        return _Done(saved)
                    if latest.dispatched_at is not None:
                        return _Stopped(
                            _stop(
                                ResearchStopCode.AMBIGUOUS_CALL,
                                f"{call.value} call {latest.call_seq} may have been sent and has "
                                "no outcome; reconcile it by hand before resuming",
                            )
                        )
                    record = latest  # 呼んでいない証拠。dispatch から続ける
            if record is None:
                key = call_idempotency_key(request_id, call, input_hash, len(mine) + 1)
                try:
                    record = await calls.reserve(
                        request_id=request_id,
                        call=call,
                        idempotency_key=key,
                        input_hash=input_hash,
                        provider=provider_label[:_PROVIDER_LABEL_MAX],
                        estimated_cost_usd=self._cost.usd_for(call),
                        quota_units=quota_units,
                    )
                except ResearchBudgetExceededError as exc:
                    await session.rollback()
                    return _Stopped(_stop(ResearchStopCode.CALL_BUDGET_EXHAUSTED, str(exc)))
                except ResearchAmbiguousCallError as exc:
                    await session.rollback()
                    return _Stopped(_stop(ResearchStopCode.AMBIGUOUS_CALL, str(exc)))
                await session.commit()

            try:
                await calls.mark_dispatched(record.id)
            except ResearchAmbiguousCallError as exc:
                await session.rollback()
                return _Stopped(_stop(ResearchStopCode.AMBIGUOUS_CALL, str(exc)))
            await session.commit()

            try:
                value = await invoke()
            except asyncio.CancelledError:
                raise  # 呼んだかもしれない: dispatch 済みの reserved のまま（成否不明）
            except Exception as exc:
                handling = _classify_exception(exc)  # 分類できない例外はここで送出される
                prefix = (
                    _TRANSIENT
                    if handling is None
                    else _STOPPED
                    if isinstance(handling, StopReason)
                    else _PERMANENT
                )
                await calls.mark_spent(record.id, error_summary=f"{prefix}{type(exc).__name__}")
                await session.commit()
                if handling is None:
                    raise ResearchSourceUnavailableError(
                        f"{call.value} call failed transiently ({type(exc).__name__})"
                    ) from exc
                if isinstance(handling, StopReason):
                    return _Stopped(handling)
                return _Failed(handling)

            handling = classify_value(value)
            if handling is None or isinstance(handling, StopReason):
                # 一時障害・rate limit の値: 結果として使わない（生データを保存しない）
                prefix = _TRANSIENT if handling is None else _STOPPED
                await calls.mark_spent(record.id, error_summary=f"{prefix}failed_value")
                await session.commit()
                if handling is None:
                    raise ResearchSourceUnavailableError(f"{call.value} call failed transiently")
                return _Stopped(handling)
            await self._raw.save(codec, request_id, record.id, value)
            await calls.mark_spent(record.id, error_summary=handling or None)
            await session.commit()
            return _Done(value)

    # ------------------------------------------------------------------ 確定

    def _validate(
        self, loaded: _Loaded, handler: ResearchHandler, output: HandlerOutput
    ) -> dict[str, Any]:
        if output.artifact_type is not handler.artifact_type:
            raise ResearchOutputInvalidError(
                f"handler for {handler.kind.value} returned a {output.artifact_type.value} artifact"
            )
        payload = dict(output.artifact)
        if payload.get("request_id") != loaded.request.id:
            raise ResearchOutputInvalidError("artifact request_id does not match the request")
        if not output.schema_version or len(output.schema_version) > 16:
            raise ResearchOutputInvalidError("artifact schema_version must be 1..16 characters")
        return payload

    async def _finalize(
        self,
        loaded: _Loaded,
        handler: ResearchHandler,
        output: HandlerOutput,
        status: ResearchStatus,
        stop: StopReason | None,
        warnings: tuple[str, ...],
    ) -> ResearchExecution:
        request_id = loaded.request.id
        payload = self._validate(loaded, handler, output)
        try:
            sha = sha256_hex(canonical_json_bytes(payload))
        except (TypeError, ValueError) as exc:
            raise ResearchOutputInvalidError("artifact is not canonical JSON") from exc
        key = research_artifact_object_key(request_id, output.artifact_type, sha)
        put = await self._store.put_json(key, payload)
        stored = await readback_sha256(self._store, key)
        if put.sha256 != sha or stored != sha:
            raise ResearchArtifactReadbackError(
                f"research artifact readback mismatch for {output.artifact_type.value}"
            )

        async with self._session_factory() as session:
            record = await ResearchArtifactRepository(session).record(
                request_id=request_id,
                artifact_type=output.artifact_type,
                schema_version=output.schema_version,
                bucket=self._bucket,
                object_key=key,
                sha256=sha,
                size_bytes=put.size,
            )
            result = ResearchResult(
                request_id=request_id,
                execution_status=status,
                artifact_refs=(
                    ResearchArtifactRef(
                        artifact_type=output.artifact_type, artifact_id=record.id, sha256=sha
                    ),
                ),
                coverage=output.coverage,
                warnings=warnings,
                usage=await self._usage(session, request_id),
                stop_code=stop.code if stop else None,
            )
            finished = await ResearchRequestRepository(session).finish(
                request_id, status, result=result
            )
            await session.commit()
        return self._outcome_of(finished)

    async def _finish_without_artifact(
        self, loaded: _Loaded, status: ResearchStatus, reason: StopReason
    ) -> ResearchExecution:
        """``blocked`` / ``failed`` の記録。すでに終わっている依頼は書き換えない。"""
        request_id = loaded.request.id
        async with self._session_factory() as session:
            requests = ResearchRequestRepository(session)
            current = await requests.get(request_id)
            if current is None:
                raise ResearchInputInvalidError(f"research request not found: {request_id}")
            if current.status in _FINISHED:
                return self._outcome_of(current)
            await requests.mark_running(request_id)
            result = ResearchResult(
                request_id=request_id,
                execution_status=status,
                coverage=ResearchCoverage(items_requested=0, items_covered=0),
                warnings=(_clip(reason.detail),),
                usage=await self._usage(session, request_id),
                stop_code=reason.code,
            )
            finished = await requests.finish(
                request_id,
                status,
                result=result,
                blocked_reason=(
                    reason.as_blocked_reason() if status is ResearchStatus.BLOCKED else None
                ),
            )
            await session.commit()
        return self._outcome_of(finished)

    # ------------------------------------------------------------------ 補助

    @staticmethod
    async def _usage(session: AsyncSession, request_id: str) -> ResearchUsage:
        """台帳から数える使用量（``abandoned`` は費用が発生していないので数えない）。"""
        rows = [
            c
            for c in await ResearchCallRepository(session).list_for_request(request_id)
            if c.status is not ResearchCallStatus.ABANDONED
        ]
        cost = sum((c.estimated_cost_usd or Decimal(0) for c in rows), Decimal(0))
        return ResearchUsage(
            searches=sum(1 for c in rows if c.call is ResearchCall.SEARCH),
            fetches=sum(1 for c in rows if c.call is ResearchCall.FETCH),
            assessments=sum(1 for c in rows if c.call is ResearchCall.ASSESS),
            youtube_units=sum(c.quota_units or 0 for c in rows),
            cost_usd=cost.quantize(Decimal("0.0001")),
        )

    @staticmethod
    def _outcome_of(request: ResearchRequest) -> ResearchExecution:
        """保存済みの依頼から結果を作る（再実行が同じ結果を返す）。"""
        result = (
            ResearchResult.model_validate(request.result_summary)
            if request.result_summary
            else None
        )
        stop_code = result.stop_code.value if result and result.stop_code else None
        if stop_code is None and request.blocked_reason:
            stop_code = request.blocked_reason.get("code")
        return ResearchExecution(
            request_id=request.id,
            status=request.status,
            artifact_refs=result.artifact_refs if result else (),
            stop_code=stop_code,
            usage=result.usage if result else ResearchUsage(),
        )

    @staticmethod
    def _warnings(
        output: HandlerOutput,
        searches_skipped: Sequence[str],
        fetches_skipped: Sequence[str],
        failures: Sequence[str],
        stop: StopReason | None,
    ) -> tuple[str, ...]:
        lines = [*output.warnings]
        if stop is not None:
            lines.append(f"{stop.code.value}: {stop.detail}")
        if searches_skipped:
            lines.append(f"searches skipped: {', '.join(searches_skipped)}")
        if fetches_skipped:
            lines.append(f"fetches skipped: {', '.join(fetches_skipped)}")
        lines.extend(failures)
        return tuple(_clip(line) for line in lines if line.strip())[:_MAX_WARNINGS]
