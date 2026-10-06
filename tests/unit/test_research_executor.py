"""Research の実行器（ADR-0037 §4 / §5 / §8）。

SQLite・インメモリの ArtifactStore・Fake Provider で走る。

守るもの:
- 外部呼び出しはすべて ``reserve → dispatch → spent`` で台帳に残る
  （呼ばなかった予約だけ ``abandoned``）
- 上限（件数・quota）に達したら止めて ``partial``。上限を越えて呼ばない（INV-36）
- 一時障害は retryable を投げる。予約は ``reserved`` のまま残さない。再実行は成功済みの呼び出しを
  送り直さず、成否不明の呼び出しは送り直さない
- 成果物は research のキーに書き、読み戻して sha256 を照合してから記録する

理由は docs/testing/research-execution-rationale.md。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from contracts.research import (
    ResearchArtifactType,
    ResearchCall,
    ResearchCallStatus,
    ResearchKind,
    ResearchStatus,
    parse_research_spec,
)
from domain.research.errors import (
    ResearchArtifactReadbackError,
    ResearchSourceUnavailableError,
)
from domain.research.keys import research_artifact_object_key
from domain.research.ports import SearchQuery, SearchResults
from infrastructure.db.models import ResearchArtifactRow, ResearchCallRow
from infrastructure.db.research_repositories import (
    ResearchArtifactRepository,
    ResearchCallRepository,
    ResearchRequestRepository,
)
from infrastructure.research.errors import (
    ProviderAuthRejected,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTransient,
)
from infrastructure.research.executor import (
    ResearchExecutor,
    call_idempotency_key,
    query_hash,
)
from infrastructure.research.fake_providers import FakeContentFetcher, FakeSearchProvider
from infrastructure.research.quota_costs import YOUTUBE_FULL_SEARCH_UNITS
from infrastructure.research.raw_store import SEARCH_CODEC, ResearchRawStore
from infrastructure.research.registry import CostModel, ResearchProviders
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.research import evidence_payload
from tests.support.research_handlers import GenericTestHandler

PCV = "provider-config-1+fake"
TWO = ("明治維新", "鉄砲伝来")


class _FailingSearch(FakeSearchProvider):
    """指定した検索語の最初の ``times`` 回だけ ``error`` を投げる（それ以外は Fake のまま）。"""

    def __init__(self, text: str, error: BaseException, times: int = 1) -> None:
        super().__init__()
        self._text, self._error, self._left = text, error, times

    async def search(self, query: SearchQuery) -> SearchResults:
        if query.text == self._text and self._left > 0:
            self._left -= 1
            self.calls.append(query)
            raise self._error
        return await super().search(query)


class _CorruptReadback(InMemoryArtifactStore):
    """書き込みは正しいが、読み戻すと違う sha256 を返す（保存物の破損・取り違え）。"""

    async def sha256_of(self, key: str) -> str:
        await super().sha256_of(key)
        return "0" * 64


async def _request(session_factory, limits: dict | None = None, key: str = "evidence:1") -> str:
    payload = evidence_payload(limits=limits) if limits else evidence_payload()
    async with session_factory() as session:
        created = await ResearchRequestRepository(session).create_or_get(
            idempotency_key=key, spec=parse_research_spec(payload), provider_config_version=PCV
        )
        await session.commit()
    return created.id


def _executor(
    session_factory,
    store,
    *,
    handler: GenericTestHandler | None = None,
    search: FakeSearchProvider | None = None,
    fetcher: FakeContentFetcher | None = None,
    providers: ResearchProviders | None = None,
    clock=None,
) -> ResearchExecutor:
    handler = handler or GenericTestHandler(TWO)
    return ResearchExecutor(
        session_factory=session_factory,
        store=store,
        bucket="artifacts",
        providers=providers
        or ResearchProviders(
            mode="fake",
            search=search or FakeSearchProvider(),
            fetcher=fetcher or FakeContentFetcher(),
            is_real=False,
        ),
        handlers={handler.kind: handler},
        cost_model=CostModel(),
        clock=clock or (lambda: datetime.now(UTC)),
    )


async def _calls(session_factory, request_id: str):
    async with session_factory() as session:
        return await ResearchCallRepository(session).list_for_request(request_id)


async def _count(session_factory, row) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(row)) or 0)


# ------------------------------------------------------------------ 正常系


async def test_every_external_call_goes_through_the_ledger_and_the_request_completes(
    session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)
    search, fetcher = FakeSearchProvider(), FakeContentFetcher()
    outcome = await _executor(
        session_factory, artifact_store, search=search, fetcher=fetcher
    ).execute(request_id)

    assert outcome.status is ResearchStatus.COMPLETED
    calls = await _calls(session_factory, request_id)
    assert [(c.call, c.call_seq) for c in calls] == [
        (ResearchCall.FETCH, 1),
        (ResearchCall.FETCH, 2),
        (ResearchCall.SEARCH, 1),
        (ResearchCall.SEARCH, 2),
    ]
    assert all(c.status is ResearchCallStatus.SPENT and c.dispatched_at for c in calls)
    assert [q.text for q in search.calls] == list(TWO)
    assert len(fetcher.calls) == 2  # URL の取得は注入された fetcher だけを通る
    assert (outcome.usage.searches, outcome.usage.fetches) == (2, 2)


async def test_the_artifact_is_written_under_the_research_key_read_back_and_recorded(
    session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)
    outcome = await _executor(session_factory, artifact_store).execute(request_id)

    (ref,) = outcome.artifact_refs
    async with session_factory() as session:
        record = await ResearchArtifactRepository(session).find_current(
            request_id, ResearchArtifactType.RESEARCH_EVIDENCE
        )
        request = await ResearchRequestRepository(session).get(request_id)
    assert record is not None and record.id == ref.artifact_id and record.sha256 == ref.sha256
    assert record.object_key == research_artifact_object_key(
        request_id, ResearchArtifactType.RESEARCH_EVIDENCE, ref.sha256
    )
    assert await artifact_store.sha256_of(record.object_key) == record.sha256
    stored = await artifact_store.get_json(record.object_key)
    assert stored["request_id"] == request_id
    assert request is not None and request.status is ResearchStatus.COMPLETED
    assert request.result_summary is not None
    assert request.result_summary["artifact_refs"][0]["sha256"] == ref.sha256


async def test_re_executing_a_finished_request_returns_its_outcome_without_calling(
    session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)
    first = await _executor(session_factory, artifact_store).execute(request_id)
    search = FakeSearchProvider()
    again = await _executor(session_factory, artifact_store, search=search).execute(request_id)
    assert again.status is first.status
    assert again.artifact_refs == first.artifact_refs
    assert search.calls == []


# ------------------------------------------------------------------ 呼ぶ前に止める（fail-closed）


async def test_provider_none_blocks_before_any_call(session_factory, artifact_store) -> None:
    request_id = await _request(session_factory)
    none = ResearchProviders(mode="none", search=None, fetcher=None, is_real=False)
    outcome = await _executor(session_factory, artifact_store, providers=none).execute(request_id)
    assert outcome.status is ResearchStatus.BLOCKED
    assert outcome.stop_code == "provider_not_configured"
    assert await _count(session_factory, ResearchCallRow) == 0


async def test_a_real_provider_without_budget_is_blocked_by_the_executor_too(
    session_factory, artifact_store
) -> None:
    """Gateway を経由しない依頼でも、実行器が同じ判定（1 か所の関数）で止める。"""
    request_id = await _request(session_factory)
    search = FakeSearchProvider()
    real = ResearchProviders(
        mode="real-stub", search=search, fetcher=FakeContentFetcher(), is_real=True
    )
    outcome = await _executor(session_factory, artifact_store, providers=real).execute(request_id)
    assert outcome.status is ResearchStatus.BLOCKED and outcome.stop_code == "budget_not_set"
    assert search.calls == []
    assert await _count(session_factory, ResearchCallRow) == 0


async def test_a_kind_without_a_handler_is_blocked(session_factory, artifact_store) -> None:
    request_id = await _request(session_factory)
    trend_only = GenericTestHandler(TWO, kind=ResearchKind.TREND)
    outcome = await _executor(session_factory, artifact_store, handler=trend_only).execute(
        request_id
    )
    assert outcome.status is ResearchStatus.BLOCKED
    assert outcome.stop_code == "handler_not_available"


# ------------------------------------------------------------------ 上限（INV-36）→ partial


async def test_searches_planned_beyond_the_ceiling_are_not_sent_and_the_result_is_partial(
    session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory, limits={"max_searches": 1})
    search = FakeSearchProvider()
    outcome = await _executor(session_factory, artifact_store, search=search).execute(request_id)
    assert outcome.status is ResearchStatus.PARTIAL
    assert [q.text for q in search.calls] == ["明治維新"]
    async with session_factory() as session:
        request = await ResearchRequestRepository(session).get(request_id)
    assert request is not None and request.result_summary is not None
    assert any("s02" in w for w in request.result_summary["warnings"])


async def test_reaching_the_ledger_ceiling_stops_the_calls_and_finishes_partial(
    session_factory, artifact_store
) -> None:
    """再試行で使った番号も枠を数えるので、2 回目の実行で枠が尽きたら止めて ``partial`` にする。"""
    request_id = await _request(session_factory, limits={"max_searches": 2})
    search = _FailingSearch("明治維新", ProviderTransient("5xx"))
    executor = _executor(session_factory, artifact_store, search=search)
    with pytest.raises(ResearchSourceUnavailableError):
        await executor.execute(request_id)

    outcome = await executor.execute(request_id)
    assert outcome.status is ResearchStatus.PARTIAL
    assert outcome.stop_code == "call_budget_exhausted"
    searches = [
        c for c in await _calls(session_factory, request_id) if c.call is ResearchCall.SEARCH
    ]
    assert [c.call_seq for c in searches] == [1, 2]
    assert [q.text for q in search.calls] == ["明治維新", "明治維新"]  # 鉄砲伝来は送っていない


async def test_the_quota_budget_is_estimated_before_the_call(
    session_factory, artifact_store
) -> None:
    request_id = await _request(
        session_factory, limits={"max_youtube_units": YOUTUBE_FULL_SEARCH_UNITS + 1}
    )
    handler = GenericTestHandler(("関ヶ原", "鉄砲伝来"), search_kind="youtube", fetch_per_search=0)
    search = FakeSearchProvider()
    outcome = await _executor(
        session_factory, artifact_store, handler=handler, search=search
    ).execute(request_id)
    assert outcome.status is ResearchStatus.PARTIAL
    assert outcome.stop_code == "call_budget_exhausted"
    assert len(search.calls) == 1
    (only,) = await _calls(session_factory, request_id)
    assert only.quota_units == YOUTUBE_FULL_SEARCH_UNITS
    assert outcome.usage.youtube_units == YOUTUBE_FULL_SEARCH_UNITS


# ------------------------------------------------------------------ 一時障害: retry と台帳


async def test_a_transient_failure_raises_retryable_without_leaving_a_reservation(
    session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)
    search = _FailingSearch("鉄砲伝来", ProviderTransient("connection reset"))
    executor = _executor(session_factory, artifact_store, search=search)
    with pytest.raises(ResearchSourceUnavailableError):
        await executor.execute(request_id)

    calls = await _calls(session_factory, request_id)
    assert all(c.status is not ResearchCallStatus.RESERVED for c in calls)
    failed = [c for c in calls if c.error_summary]
    assert len(failed) == 1 and failed[0].status is ResearchCallStatus.SPENT  # 呼んだので課金扱い

    outcome = await executor.execute(request_id)  # Temporal の retry に相当
    assert outcome.status is ResearchStatus.COMPLETED
    texts = [q.text for q in search.calls]
    assert texts.count("明治維新") == 1  # 成功済みの検索は生データから読み、送り直さない
    assert texts.count("鉄砲伝来") == 2
    searches = [
        c for c in await _calls(session_factory, request_id) if c.call is ResearchCall.SEARCH
    ]
    assert [c.call_seq for c in searches] == [1, 2, 3]


async def test_a_transient_fetch_failure_is_retried_in_a_new_round(
    session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)
    fetcher = FakeContentFetcher()
    fetcher.fail_next_with("timeout")
    executor = _executor(session_factory, artifact_store, fetcher=fetcher)
    with pytest.raises(ResearchSourceUnavailableError):
        await executor.execute(request_id)
    outcome = await executor.execute(request_id)
    assert outcome.status is ResearchStatus.COMPLETED
    fetches = [c for c in await _calls(session_factory, request_id) if c.call is ResearchCall.FETCH]
    assert [c.status for c in fetches] == [ResearchCallStatus.SPENT] * 3


# ------------------------------------------------------------------ 成否不明の呼び出し


async def _dispatched_without_outcome(session_factory, request_id: str, text: str) -> str:
    query = SearchQuery(text=text, kind="web", max_results=3)
    async with session_factory() as session:
        await ResearchRequestRepository(session).mark_running(request_id)
        calls = ResearchCallRepository(session)
        call = await calls.reserve(
            request_id=request_id,
            call=ResearchCall.SEARCH,
            idempotency_key=call_idempotency_key(
                request_id, ResearchCall.SEARCH, query_hash(query), 1
            ),
            input_hash=query_hash(query),
            provider="fake",
        )
        await session.commit()
        await calls.mark_dispatched(call.id)
        await session.commit()
    return call.id


async def test_an_ambiguous_call_is_not_resent_and_blocks_the_request(
    session_factory, artifact_store
) -> None:
    """dispatch 済みで結果が無い呼び出し（送信後のクラッシュ）を送り直さない・解放しない。"""
    request_id = await _request(session_factory)
    call_id = await _dispatched_without_outcome(session_factory, request_id, "明治維新")
    search = FakeSearchProvider()
    outcome = await _executor(session_factory, artifact_store, search=search).execute(request_id)

    assert outcome.status is ResearchStatus.BLOCKED
    assert outcome.stop_code == "ambiguous_call"
    assert "明治維新" not in [q.text for q in search.calls]
    async with session_factory() as session:
        (ambiguous,) = await ResearchCallRepository(session).find_ambiguous(request_id)
    assert ambiguous.id == call_id and ambiguous.status is ResearchCallStatus.RESERVED


async def test_a_call_whose_raw_output_was_saved_is_settled_without_resending(
    session_factory, artifact_store
) -> None:
    """呼んで生データを保存した後・``spent`` の前に落ちた: 保存物から続け、送り直さない。"""
    request_id = await _request(session_factory)
    call_id = await _dispatched_without_outcome(session_factory, request_id, "明治維新")
    earlier = await FakeSearchProvider().search(
        SearchQuery(text="明治維新", kind="web", max_results=3)
    )
    await ResearchRawStore(artifact_store).save(SEARCH_CODEC, request_id, call_id, earlier)

    search = FakeSearchProvider()
    outcome = await _executor(session_factory, artifact_store, search=search).execute(request_id)
    assert outcome.status is ResearchStatus.COMPLETED
    assert [q.text for q in search.calls] == ["鉄砲伝来"]
    calls = await _calls(session_factory, request_id)
    assert next(c for c in calls if c.id == call_id).status is ResearchCallStatus.SPENT


async def test_an_unclassified_error_leaves_the_call_ambiguous_and_it_is_never_resent(
    session_factory, artifact_store
) -> None:
    """分類できない例外は握りつぶさない。呼んだかもしれないので、再実行は送り直さずに止める。"""
    request_id = await _request(session_factory)
    search = _FailingSearch("明治維新", RuntimeError("adapter bug"))
    executor = _executor(session_factory, artifact_store, search=search)
    with pytest.raises(RuntimeError):
        await executor.execute(request_id)
    outcome = await executor.execute(request_id)
    assert outcome.status is ResearchStatus.BLOCKED and outcome.stop_code == "ambiguous_call"
    assert [q.text for q in search.calls] == ["明治維新"]


async def test_a_permanently_rejected_search_is_not_resent_when_the_request_is_retried(
    session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)

    class _Mixed(FakeSearchProvider):
        transient_left = 1

        async def search(self, query: SearchQuery) -> SearchResults:
            self.calls.append(query)
            if query.text == "明治維新":
                raise ProviderRejected("400 bad query")
            if self.transient_left:
                self.transient_left -= 1
                raise ProviderTransient("503")
            self.calls.pop()
            return await super().search(query)

    search = _Mixed()
    executor = _executor(session_factory, artifact_store, search=search)
    with pytest.raises(ResearchSourceUnavailableError):
        await executor.execute(request_id)
    outcome = await executor.execute(request_id)
    assert outcome.status is ResearchStatus.PARTIAL
    assert [q.text for q in search.calls].count("明治維新") == 1


# ------------------------------------------------------------------ quota / rate limit / 認証


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ProviderRateLimited("429"), "rate_limited"),
        (ProviderQuotaExhausted("quota"), "quota_exhausted"),
        (ProviderAuthRejected("401"), "provider_auth"),
    ],
)
async def test_quota_rate_limit_and_auth_stop_the_calls_and_block_when_nothing_is_usable(
    session_factory, artifact_store, error: Exception, code: str
) -> None:
    request_id = await _request(session_factory)
    search = _FailingSearch("明治維新", error)
    outcome = await _executor(session_factory, artifact_store, search=search).execute(request_id)
    assert outcome.status is ResearchStatus.BLOCKED
    assert outcome.stop_code == code
    assert [q.text for q in search.calls] == ["明治維新"]  # 止めた後は呼ばない
    (only,) = await _calls(session_factory, request_id)
    assert only.status is ResearchCallStatus.SPENT


async def test_a_stop_after_usable_results_finishes_partial(
    session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)
    search = _FailingSearch("鉄砲伝来", ProviderRateLimited("429"))
    outcome = await _executor(session_factory, artifact_store, search=search).execute(request_id)
    assert outcome.status is ResearchStatus.PARTIAL
    assert outcome.stop_code == "rate_limited"
    assert outcome.artifact_refs


async def test_a_permanently_failed_fetch_is_a_gap_and_the_result_is_partial(
    session_factory, artifact_store
) -> None:
    request_id = await _request(session_factory)
    fetcher = FakeContentFetcher()
    fetcher.fail_next_with("http_4xx", 404)
    outcome = await _executor(session_factory, artifact_store, fetcher=fetcher).execute(request_id)
    assert outcome.status is ResearchStatus.PARTIAL
    assert len(fetcher.calls) == 2  # 恒久的な失敗は同じ URL を送り直さない


async def test_the_deadline_stops_further_calls(session_factory, artifact_store) -> None:
    request_id = await _request(session_factory, limits={"deadline_seconds": 60})
    search = FakeSearchProvider()
    late = datetime.now(UTC) + timedelta(hours=1)
    outcome = await _executor(
        session_factory, artifact_store, search=search, clock=lambda: late
    ).execute(request_id)
    assert outcome.status is ResearchStatus.FAILED
    assert outcome.stop_code == "deadline_exceeded"
    assert search.calls == []


# ------------------------------------------------------------------ 成果物の読み戻し


async def test_a_readback_mismatch_records_nothing_and_is_retryable(session_factory) -> None:
    request_id = await _request(session_factory)
    corrupt = _CorruptReadback()
    with pytest.raises(ResearchArtifactReadbackError):
        await _executor(session_factory, corrupt).execute(request_id)
    assert await _count(session_factory, ResearchArtifactRow) == 0
    async with session_factory() as session:
        request = await ResearchRequestRepository(session).get(request_id)
    assert request is not None and request.status is ResearchStatus.RUNNING

    healthy = InMemoryArtifactStore()
    healthy._objects.update(corrupt._objects)  # 同じ保存先を正常に読めるようになった
    search = FakeSearchProvider()
    outcome = await _executor(session_factory, healthy, search=search).execute(request_id)
    assert outcome.status is ResearchStatus.COMPLETED
    assert search.calls == []  # 検索・取得は生データから読み、送り直さない


# ------------------------------------------------------------------ retry を使い切った後の記録


@pytest.mark.parametrize(
    ("error_type", "status"),
    [
        ("ResearchSourceUnavailableError", ResearchStatus.FAILED),
        ("ResearchAmbiguousCallError", ResearchStatus.BLOCKED),
        ("SomethingUnknown", ResearchStatus.BLOCKED),
    ],
)
async def test_record_failure_classifies_by_the_research_type_name(
    session_factory, artifact_store, error_type: str, status: ResearchStatus
) -> None:
    request_id = await _request(session_factory)
    outcome = await _executor(session_factory, artifact_store).record_failure(
        request_id, error_type=error_type, summary="retries exhausted"
    )
    assert outcome.status is status
    assert outcome.stop_code == "execution_failed"
    again = await _executor(session_factory, artifact_store).record_failure(
        request_id, error_type=error_type, summary="twice"
    )
    assert again.status is status  # 再実行しても状態を書き換えない
