"""Agent Tool の契約（ADR-0047 §1）。tool の定義・状態・出力の包みの唯一の宣言元（INV-62）。

- tool は read-only だけ（``ToolEffect`` に ``read`` しか無い。INV-61）。
- 結果は例外ではなく ``ToolStatus`` で返す。取得できない値を架空の値で埋めない（INV-58）。
- 外部由来の内容は ``untrusted``。指示・tool 名・URL の生成に使わない（INV-60）。
- JSON Schema は model から導出する（手書きしない）。
- 個々の tool の入出力 model は ``contracts/agent_tools_*.py`` に置き、
  ``contracts/agent_tool_registry.py`` が束ねる（このファイルは型と語彙だけ）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: tool_call_id の名前空間（変えると既存の ID が変わる。変更しない）
TOOL_NAMESPACE = uuid.UUID("5b0f6c5e-0c1c-5f3e-9a41-3d6f2b8a7e10")


class ToolId(StrEnum):
    SEARCH_TRENDS = "search_trends"
    SEARCH_HISTORICAL_FACTS = "search_historical_facts"
    FETCH_SOURCES = "fetch_sources"
    GET_VIDEO_ANALYTICS = "get_video_analytics"
    GET_AUDIENCE_RETENTION = "get_audience_retention"
    COMPARE_VIDEO_PERFORMANCE = "compare_video_performance"
    GET_AUDIENCE_DEMOGRAPHICS = "get_audience_demographics"
    GET_CHANNEL_PERFORMANCE = "get_channel_performance"


class ToolEffect(StrEnum):
    #: 外部の状態を変えない。これ以外の effect は契約として存在しない（INV-61）
    READ = "read"


CostClass = Literal["none", "research_ledger", "youtube_quota"]


class ToolStatus(StrEnum):
    OK = "ok"
    #: 応答は正常だが行が無い
    NO_DATA = "no_data"
    #: 標本・期間が足りず、結論を出さない
    INSUFFICIENT_DATA = "insufficient_data"
    #: privacy 閾値で隠れた可能性（0 とは区別する）
    WITHHELD_BY_THRESHOLD = "withheld_by_threshold"
    #: 直近日が未確定
    REPORTING_DELAY = "reporting_delay"
    #: 公式 API では取得できない指標
    NOT_AVAILABLE_VIA_API = "not_available_via_api"
    PERMISSION_DENIED = "permission_denied"
    QUOTA_EXCEEDED = "quota_exceeded"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    #: Research が未完。依頼は走り続ける
    PENDING = "pending"
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    NOT_SUPPORTED = "not_supported"
    FAILED = "failed"


#: ``agent_tool_calls.status`` の「実行前に commit した行」。結果はまだ無い（INV-59）
TOOL_CALL_STARTED = "started"
#: ``agent_tool_calls.status`` の語彙（CHECK の宣言元。migration 0020 は literal で凍結する）
TOOL_CALL_STATUSES: tuple[str, ...] = (TOOL_CALL_STARTED, *(s.value for s in ToolStatus))


class ToolDef:
    """版付きの tool 定義。コードで登録し、plugin・import 文字列の取得はしない。"""

    __slots__ = (
        "tool_id",
        "version",
        "description",
        "input_model",
        "output_model",
        "effect",
        "timeout_seconds",
        "max_calls_per_run",
        "cost_class",
    )

    def __init__(
        self,
        *,
        tool_id: ToolId,
        version: int,
        description: str,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        timeout_seconds: int,
        max_calls_per_run: int,
        cost_class: CostClass,
        effect: ToolEffect = ToolEffect.READ,
    ) -> None:
        if version < 1:
            raise ValueError("tool version must be >= 1")
        if timeout_seconds < 1 or max_calls_per_run < 1:
            raise ValueError("timeout_seconds and max_calls_per_run must be >= 1")
        self.tool_id = tool_id
        self.version = version
        self.description = description
        self.input_model = input_model
        self.output_model = output_model
        self.effect = ToolEffect(effect)
        self.timeout_seconds = timeout_seconds
        self.max_calls_per_run = max_calls_per_run
        self.cost_class = cost_class

    @property
    def ref(self) -> str:
        return tool_ref(self.tool_id, self.version)

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema()


def tool_ref(tool_id: ToolId | str, version: int) -> str:
    return f"{ToolId(tool_id).value}@{version}"


def parse_tool_ref(ref: str) -> tuple[ToolId, int]:
    name, sep, ver = ref.partition("@")
    if not sep or not ver.isdigit() or int(ver) < 1:
        raise ValueError(f"tool ref must be 'tool_id@version': {ref!r}")
    return ToolId(name), int(ver)


def input_sha256(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tool_call_id(run_key: str, seq: int, tool_id: ToolId | str, input_sha: str) -> str:
    """retry で handler が同じ順序・同じ入力を作る限り同じ ID になる（INV-59）。"""
    name = f"{run_key}:{seq}:{ToolId(tool_id).value}:{input_sha}"
    return str(uuid.uuid5(TOOL_NAMESPACE, name))


class ToolEnvelope(BaseModel):
    """全 tool の出力の包み。``data`` は ``ok`` のときだけ持つ。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str
    tool_id: ToolId
    tool_version: int = Field(ge=1)
    status: ToolStatus
    data: dict[str, Any] | None = None
    #: 機械可読な制約・注意（例 ``provider=fake``、``audience_is_hypothesis``）
    limitations: tuple[str, ...] = Field(default=(), max_length=20)
    #: ``kind:id``（例 ``research_request:<id>``、``analytics_query:<sha>``）
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=50)
    retrieved_at: str | None = None
    #: 外部由来。LLM へは <data> で区切って渡し、指示として扱わない（INV-60）
    untrusted: Literal[True] = True

    @model_validator(mode="after")
    def _data_only_when_ok(self) -> ToolEnvelope:
        if (self.status is ToolStatus.OK) != (self.data is not None):
            raise ValueError("data must be present exactly when status is ok")
        return self
