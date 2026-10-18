"""tool 呼び出しの保存 port（ADR-0047 §3 / §6、INV-59）。

``ToolGateway`` は実行前に ``begin``（``started`` で commit）、完了で ``complete`` を呼ぶ。
retry は ``get`` で保存済みの結果を受け取り、**外部を呼ばない**。実体は infrastructure
（``agent_tool_calls`` + MinIO）。結果の本体は envelope（外部由来 = untrusted）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from contracts.agent_tools import ToolEnvelope


@dataclass(frozen=True, slots=True)
class ToolCallStart:
    tool_call_id: str
    run_key: str
    episode_id: str
    agent_role: str
    agent_spec_version: str
    seq: int
    tool_id: str
    tool_version: int
    input_sha256: str
    #: 検証済みの tool 入力（外部由来の本文ではない）
    tool_input: dict[str, Any]
    started_at: datetime


@dataclass(frozen=True, slots=True)
class StoredToolCall:
    tool_call_id: str
    #: ``started`` のとき ``None``（結果が無い = 呼んだか不明）
    envelope: ToolEnvelope | None
    research_request_id: str | None = None
    error_code: str | None = None

    @property
    def completed(self) -> bool:
        return self.envelope is not None


@dataclass(frozen=True, slots=True)
class ToolCallCompletion:
    envelope: ToolEnvelope
    finished_at: datetime
    duration_ms: int
    research_request_id: str | None = None
    error_code: str | None = None


class ToolCallStore(Protocol):
    async def get(self, tool_call_id: str) -> StoredToolCall | None: ...

    async def begin(self, start: ToolCallStart) -> None:
        """``started`` の行を commit する。同じ ``tool_call_id`` が既にあれば何もしない。"""
        ...

    async def complete(self, tool_call_id: str, completion: ToolCallCompletion) -> None:
        """結果を保存して行を完了にする。"""
        ...
