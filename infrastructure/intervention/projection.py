"""判断・介入イベント（``episode_events``）のログへの投影と再投影（ADR-0041 §7、設計 §14/§15）。

**正本は PostgreSQL の ``episode_events``**。ログ（stdout → Collector → OpenSearch）は
検索用の投影で、業務の判断に使わない（INV-38）。ここは OpenSearch を読まない・import しない。

- **投影（live）**: ``InterventionStore.append_event`` が ``defer_event`` で session に積み、
  その commit が成功した後にだけ1行出す（``infrastructure.logging.ledger.defer``）。
  rollback では出ない。stdout へ書けたかは分からない（ログの故障は握る）ので
  ``projected_at`` は付けない。
- **再投影**: ``reproject`` が DB から読み直して同じ形で出し直し、出した行の
  ``projected_at`` を付ける。``event_id`` は
  ``uuid5(PROJECTION_EVENT_NAMESPACE, f"{episode_id}:{seq}")`` で、live と再投影で同じ値。
  OpenSearch の ``_id``（``create``）が同じ index 内の重複を捨てる。index（日付）を
  またぐ再投影は別文書になり得るので、検索側は ``event_id`` で重複を除く（log-contract §4）。
- ``projected_at`` は「再投影の処理が stdout へ渡した」時刻で、OpenSearch に索引された
  ことの証明ではない。

ログに出すのは ID・状態語だけ。``refs`` の値のうち参照らしいキー（``*_id`` 等）の
スカラーだけを ``attributes.refs`` に入れ、本文・prompt・入れ子は落として、キー名だけを
``attributes.refs_omitted`` に残す（INV-39）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.intervention import InterventionEvent
from contracts.log_contract import EventName, Outcome
from infrastructure.logging.emit import emit, log_guard
from infrastructure.logging.formatter import PROJECTED_EVENT_ID_KEY
from infrastructure.logging.ledger import defer

if TYPE_CHECKING:
    from infrastructure.intervention.store import EpisodeEvent

logger = logging.getLogger("avp.intervention.projection")

#: 投影の event_id の名前空間（固定値。変えると同じ行の ID が変わり、再投影が重複文書になる）
PROJECTION_EVENT_NAMESPACE = uuid.UUID("0b6b5f0e-7d1c-5b7e-9a64-2f1e4c3a8d10")

#: 正本の event_name → ログの EventName。**明示の表**（同じ文字列。網羅は unit test が検査する）
EVENT_NAMES: dict[InterventionEvent, EventName] = {
    InterventionEvent.DECISION_RECORDED: EventName.DECISION_RECORDED,
    InterventionEvent.AGENT_RUN_RECORDED: EventName.AGENT_RUN_RECORDED,
    InterventionEvent.COMMAND_RECEIVED: EventName.COMMAND_RECEIVED,
    InterventionEvent.COMMAND_FINISHED: EventName.COMMAND_FINISHED,
    InterventionEvent.COMMENT_ADDED: EventName.COMMENT_ADDED,
    InterventionEvent.HOLD_REQUESTED: EventName.HOLD_REQUESTED,
    InterventionEvent.HOLD_EFFECTIVE: EventName.HOLD_EFFECTIVE,
    InterventionEvent.CHANGE_PROPOSED: EventName.CHANGE_PROPOSED,
    InterventionEvent.CHANGE_APPLIED: EventName.CHANGE_APPLIED,
    InterventionEvent.CHANGE_CONFLICTED: EventName.CHANGE_CONFLICTED,
    InterventionEvent.VARIANT_SELECTED: EventName.VARIANT_SELECTED,
    InterventionEvent.REVIEW_INVALIDATED: EventName.REVIEW_INVALIDATED,
    InterventionEvent.REVIEW_RESOLVED: EventName.REVIEW_RESOLVED,
    InterventionEvent.CHECKPOINT_WAITING: EventName.CHECKPOINT_WAITING,
    InterventionEvent.CHECKPOINT_RELEASED: EventName.CHECKPOINT_RELEASED,
    InterventionEvent.RESUME_REQUESTED: EventName.RESUME_REQUESTED,
    InterventionEvent.RESUME_COMPLETED: EventName.RESUME_COMPLETED,
    InterventionEvent.MODE_CHANGED: EventName.MODE_CHANGED,
}

#: ``refs`` のキー → ログのトップレベル項目（検索できる相関 ID）
_TOP_LEVEL_REFS: dict[str, str] = {
    "manifest_id": "manifest_id",
    "agent_run_id": "agent_run_id",
    "decision_id": "decision_id",
    "command_id": "command_id",
    "correlation_id": "correlation_id",
    "revision_id": "artifact_id",
    "artifact_id": "artifact_id",
    "workflow_id": "workflow_id",
    "run_id": "run_id",
    "reservation_id": "reservation_id",
    "research_call_id": "research_call_id",
    "model": "model",
    "prompt_version": "prompt_version",
    "estimated_cost_usd": "estimated_cost_usd",
    "actual_cost_usd": "actual_cost_usd",
    "stage": "stage",
}
#: ``attributes.refs`` に残してよい状態語のキー（ID でないもの）
_STATE_KEYS = frozenset(
    {
        "kind",
        "status",
        "state",
        "control_state",
        "mode",
        "point",
        "waiting_point",
        "reason",
        "reject_reason",
        "outcome",
        "type",
        "command_type",
        "slot",
        "role",
        "step",
        "result",
        "started",
        "variant_group",
    }
)
_REF_SUFFIXES = ("_id", "_ids", "_seq", "_revision", "_revisions", "_slot", "_slots", "_count")
_MAX_REF_CHARS = 128
_MAX_REF_ITEMS = 50


def projection_event_id(episode_id: str, seq: int) -> str:
    """``(episode_id, seq)`` → 決定的な event_id（32桁 hex）。live・再投影で同じ値。"""
    return uuid.uuid5(PROJECTION_EVENT_NAMESPACE, f"{episode_id}:{seq}").hex


def _scalar(value: Any) -> bool:
    if isinstance(value, bool | int | float):
        return True
    return isinstance(value, str) and len(value) <= _MAX_REF_CHARS


def _ref_allowed(key: str, value: Any) -> bool:
    if not (key in _STATE_KEYS or key.endswith(_REF_SUFFIXES)):
        return False
    if _scalar(value) or value is None:
        return True
    return (
        isinstance(value, list | tuple)
        and len(value) <= _MAX_REF_ITEMS
        and all(_scalar(v) for v in value)
    )


def event_fields(event: EpisodeEvent) -> dict[str, Any]:
    """正本の1行 → ログのフィールド（``emit`` の kwargs）。"""
    refs: dict[str, Any] = {}
    omitted: list[str] = []
    fields: dict[str, Any] = {
        "episode_id": event.episode_id,
        "event_seq": event.seq,
        "actor_type": event.actor.type.value,
        "command_id": event.command_id,
    }
    for key, value in (event.refs or {}).items():
        key = str(key)
        if not _ref_allowed(key, value):
            omitted.append(key)
            continue
        refs[key] = list(value) if isinstance(value, tuple) else value
        target = _TOP_LEVEL_REFS.get(key)
        if target is not None and value is not None and fields.get(target) is None:
            fields[target] = value
    if "status" in refs and isinstance(refs["status"], str):
        fields["outcome"] = _outcome(refs["status"])
    attributes: dict[str, Any] = {"actor_id": event.actor.id}
    if refs:
        attributes["refs"] = refs
    if omitted:
        attributes["refs_omitted"] = sorted(omitted)
    fields["attributes"] = attributes
    fields[PROJECTED_EVENT_ID_KEY] = projection_event_id(event.episode_id, event.seq)
    return fields


def _outcome(status: str) -> str | None:
    mapping = {
        "applied": Outcome.SUCCEEDED.value,
        "rejected": Outcome.REJECTED.value,
        "conflict": Outcome.REJECTED.value,
        "failed": Outcome.FAILED.value,
    }
    return mapping.get(status)


@dataclass(frozen=True, slots=True)
class _Prepared:
    name: EventName
    msg: str
    args: tuple[object, ...]
    fields: dict[str, Any]


def _prepare(event: EpisodeEvent) -> _Prepared | None:
    """発行の引数を組む。**例外を外へ出さない**（INV-38。組めなければ発行しない）。"""
    try:
        return _Prepared(
            name=EVENT_NAMES[event.event_name],
            msg="%s episode=%s seq=%s",
            args=(event.event_name.value, event.episode_id, event.seq),
            fields=event_fields(event),
        )
    except Exception:
        return None


def emit_event(event: EpisodeEvent) -> None:
    """今すぐ1行出す（再投影用）。失敗は握る（INV-38）。"""
    prepared = _prepare(event)
    if prepared is None:
        return
    with log_guard():
        emit(logger, prepared.name, logging.INFO, prepared.msg, *prepared.args, **prepared.fields)


def defer_event(session: AsyncSession, event: EpisodeEvent) -> None:
    """``session`` の commit が成功したら1行出す（live の投影）。失敗は握る（INV-38）。"""
    prepared = _prepare(event)
    if prepared is None:
        return
    with log_guard():
        defer(
            session,
            logger.name,
            prepared.name,
            logging.INFO,
            prepared.msg,
            *prepared.args,
            **prepared.fields,
        )


# --------------------------------------------------------------------------- 再投影


@dataclass(frozen=True, slots=True)
class ReprojectResult:
    emitted: int
    marked: int
    episodes: int


def validate_selection(*, episode_id: str | None, from_seq: int | None, to_seq: int | None) -> None:
    """seq は Episode 内の通番なので、範囲指定は Episode を伴う。"""
    if episode_id is None and (from_seq is not None or to_seq is not None):
        raise ValueError("a seq range needs an episode_id (seq is per episode)")
    if from_seq is not None and to_seq is not None and from_seq > to_seq:
        raise ValueError("from_seq must be <= to_seq")


async def reproject(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    episode_id: str | None = None,
    from_seq: int | None = None,
    to_seq: int | None = None,
    only_unprojected: bool = True,
    batch_size: int = 500,
    dry_run: bool = False,
) -> ReprojectResult:
    """正本（DB）から読み、同じ ``event_id`` で出し直して ``projected_at`` を付ける。

    頁ごとに: 読む（lock しない）→ 出す → 別 transaction で ``projected_at`` を付けて commit。
    出した後・付ける前に落ちたら次回もう一度出す（同じ event_id なので重複は検索側で除ける）。
    ``dry_run`` は数えるだけで、出さず・付けない。
    """
    from infrastructure.intervention.store import InterventionStore

    validate_selection(episode_id=episode_id, from_seq=from_seq, to_seq=to_seq)
    emitted = marked = 0
    episodes: set[str] = set()
    after: tuple[str, int] | None = None
    while True:
        async with session_factory() as session:
            page = await InterventionStore(session).list_events_page(
                episode_id=episode_id,
                from_seq=from_seq,
                to_seq=to_seq,
                only_unprojected=only_unprojected,
                after=after,
                limit=batch_size,
            )
        if not page:
            break
        after = (page[-1].episode_id, page[-1].seq)
        by_episode: dict[str, list[int]] = {}
        for event in page:
            episodes.add(event.episode_id)
            by_episode.setdefault(event.episode_id, []).append(event.seq)
            if not dry_run:
                emit_event(event)
            emitted += 1
        if not dry_run:
            async with session_factory() as session:
                store = InterventionStore(session)
                for ep, seqs in by_episode.items():
                    await store.mark_events_projected(ep, seqs)
                await session.commit()
            marked += len(page)
        if len(page) < batch_size:
            break
    return ReprojectResult(emitted=emitted, marked=marked, episodes=len(episodes))


__all__ = [
    "EVENT_NAMES",
    "PROJECTION_EVENT_NAMESPACE",
    "ReprojectResult",
    "defer_event",
    "emit_event",
    "event_fields",
    "projection_event_id",
    "reproject",
    "validate_selection",
]
