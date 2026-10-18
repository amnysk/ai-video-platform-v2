"""Tool executor の port（ADR-0047 §3）。
実体（Research / Analytics adapter）は infrastructure が注入する。

executor は ``ToolOutcome`` を返す。**状態で返し、例外にしない**ものは ``ToolStatus`` に写す
（quota 超過・権限不足・データ無し等）。
想定外の障害だけ例外にしてよい（gateway が ``failed`` にする）。
``ToolGateway`` が tool_call_id の払い出し・保存済み結果の再利用（外部を再び呼ばない）・
timeout・記録・ログを担当し、executor は**外部呼び出しだけ**を担当する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from contracts.agent_tools import ToolStatus


@dataclass(frozen=True, slots=True)
class ToolContext:
    tool_call_id: str
    episode_id: str
    #: ``role@spec_version``（requester / 監査用）
    agent_ref: str
    run_key: str
    seq: int
    #: Research などの外部依頼の冪等キーに使う。``tool:{tool_call_id}``
    idempotency_key: str
    now: datetime


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    status: ToolStatus
    #: ``ToolDef.output_model`` のインスタンス。``status`` が ok のときだけ持つ
    data: BaseModel | None = None
    limitations: tuple[str, ...] = ()
    #: ``kind:id``
    evidence_refs: tuple[str, ...] = ()
    #: Research の依頼 ID（``agent_tool_calls.research_request_id``）
    research_request_id: str | None = None
    #: 外部から取得した時刻（ISO 8601）
    retrieved_at: str | None = None
    #: 診断用の短いコード（秘密・URL の query・本文を入れない）
    error_code: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


class ToolExecutor(Protocol):
    async def __call__(self, ctx: ToolContext, tool_input: BaseModel) -> ToolOutcome: ...
