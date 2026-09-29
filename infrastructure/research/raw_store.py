"""外部呼び出しの生データ（検索結果・本文）の保存（ADR-0037 §8）。

- 検索結果（``SearchResults``）と本文（``FetchedContent``）は JSON で research の接頭辞の
  ``research/{request_id}/raw/{call}/{call_id}.json`` にだけ置く（キーは
  ``domain/research/keys.py``。形をここで組み立てない）。成果物ではないので
  ``research_artifacts`` に記録しない
- 台帳の行 1 件につき 1 オブジェクト。保存物は「呼んで結果を受け取った」証拠で、再実行は
  これを読んで同じ呼び出しを送り直さない
- 保存は immutable（同じキーに違う内容は ``ArtifactConflictError``）。証拠を書き換えない
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from contracts.research import ResearchCall
from domain.research.keys import research_raw_object_key
from domain.research.ports import (
    FetchedContent,
    SearchHit,
    SearchQuery,
    SearchResults,
    StatObservation,
)
from infrastructure.storage.artifact_store import ArtifactStore


@dataclass(frozen=True, slots=True)
class RawCodec[T]:
    """1 種類の生データ（呼び出しの種別）と JSON 写像。"""

    call: ResearchCall
    encode: Callable[[T], dict[str, Any]]
    decode: Callable[[dict[str, Any]], T]


def _dt(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _from_dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _encode_query(query: SearchQuery) -> dict[str, Any]:
    return {
        "text": query.text,
        "kind": query.kind,
        "max_results": query.max_results,
        "region_code": query.region_code,
        "language": query.language,
        "published_after": _dt(query.published_after),
        "published_before": _dt(query.published_before),
    }


def _decode_query(data: dict[str, Any]) -> SearchQuery:
    return SearchQuery(
        text=data["text"],
        kind=data["kind"],
        max_results=data["max_results"],
        region_code=data["region_code"],
        language=data["language"],
        published_after=_from_dt(data["published_after"]),
        published_before=_from_dt(data["published_before"]),
    )


def _encode_hit(hit: SearchHit) -> dict[str, Any]:
    return {
        "url": hit.url,
        "title": hit.title,
        "snippet": hit.snippet,
        "published_at": _dt(hit.published_at),
        "provider": hit.provider,
        "provider_ref": hit.provider_ref,
        "channel_id": hit.channel_id,
        "channel_title": hit.channel_title,
        "stats": [
            {
                "metric": s.metric,
                "value": s.value,
                "unit": s.unit,
                "observed_at": s.observed_at.isoformat(),
            }
            for s in hit.stats
        ],
        "channel_subscriber_count": hit.channel_subscriber_count,
        "duration_seconds": hit.duration_seconds,
        "extras": dict(hit.extras),
    }


def _decode_hit(data: dict[str, Any]) -> SearchHit:
    return SearchHit(
        url=data["url"],
        title=data["title"],
        snippet=data["snippet"],
        published_at=_from_dt(data["published_at"]),
        provider=data["provider"],
        provider_ref=data["provider_ref"],
        channel_id=data["channel_id"],
        channel_title=data["channel_title"],
        stats=tuple(
            StatObservation(
                metric=s["metric"],
                value=s["value"],
                unit=s["unit"],
                observed_at=datetime.fromisoformat(s["observed_at"]),
            )
            for s in data["stats"]
        ),
        channel_subscriber_count=data["channel_subscriber_count"],
        duration_seconds=data["duration_seconds"],
        extras=dict(data["extras"]),
    )


def _encode_search(results: SearchResults) -> dict[str, Any]:
    return {
        "query": _encode_query(results.query),
        "hits": [_encode_hit(h) for h in results.hits],
        "searched_at": results.searched_at.isoformat(),
        "provider": results.provider,
        "cost_units": results.cost_units,
        "truncated": results.truncated,
        "warnings": list(results.warnings),
    }


def _decode_search(data: dict[str, Any]) -> SearchResults:
    return SearchResults(
        query=_decode_query(data["query"]),
        hits=tuple(_decode_hit(h) for h in data["hits"]),
        searched_at=datetime.fromisoformat(data["searched_at"]),
        provider=data["provider"],
        cost_units=data["cost_units"],
        truncated=data["truncated"],
        warnings=tuple(data["warnings"]),
    )


def _encode_fetch(content: FetchedContent) -> dict[str, Any]:
    return {
        "requested_url": content.requested_url,
        "final_url": content.final_url,
        "redirect_chain": list(content.redirect_chain),
        "status_code": content.status_code,
        "content_type": content.content_type,
        "text": content.text,
        "content_sha256": content.content_sha256,
        "bytes_read": content.bytes_read,
        "truncated": content.truncated,
        "fetched_at": content.fetched_at.isoformat(),
        "fetch_status": content.fetch_status,
        "error": content.error,
        "error_detail": content.error_detail,
    }


def _decode_fetch(data: dict[str, Any]) -> FetchedContent:
    return FetchedContent(
        requested_url=data["requested_url"],
        final_url=data["final_url"],
        redirect_chain=tuple(data["redirect_chain"]),
        status_code=data["status_code"],
        content_type=data["content_type"],
        text=data["text"],
        content_sha256=data["content_sha256"],
        bytes_read=data["bytes_read"],
        truncated=data["truncated"],
        fetched_at=datetime.fromisoformat(data["fetched_at"]),
        fetch_status=data["fetch_status"],
        error=data["error"],
        error_detail=data["error_detail"],
    )


SEARCH_CODEC: RawCodec[SearchResults] = RawCodec(
    call=ResearchCall.SEARCH, encode=_encode_search, decode=_decode_search
)
FETCH_CODEC: RawCodec[FetchedContent] = RawCodec(
    call=ResearchCall.FETCH, encode=_encode_fetch, decode=_decode_fetch
)


class ResearchRawStore:
    """``ArtifactStore`` の上の薄い層。台帳の行 1 件につき 1 オブジェクト。"""

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    @staticmethod
    def key_for(codec: RawCodec[Any], request_id: str, call_id: str) -> str:
        return research_raw_object_key(request_id, codec.call, call_id)

    async def save[T](self, codec: RawCodec[T], request_id: str, call_id: str, value: T) -> str:
        """保存してキーを返す。同じ内容の再保存は成功（immutable。違う内容は例外）。"""
        key = self.key_for(codec, request_id, call_id)
        await self._store.put_json(key, codec.encode(value))
        return key

    async def find[T](self, codec: RawCodec[T], request_id: str, call_id: str) -> T | None:
        """保存済みなら中身（呼んで結果を受け取った証拠）。無ければ ``None``。"""
        key = self.key_for(codec, request_id, call_id)
        if not await self._store.exists(key):
            return None
        return codec.decode(await self._store.get_json(key))


__all__ = ["FETCH_CODEC", "SEARCH_CODEC", "RawCodec", "ResearchRawStore"]
