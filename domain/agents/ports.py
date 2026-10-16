"""Agent が使う LLM の port（ADR-0043 §2）。実体（fake / codex）は infrastructure が注入する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class LlmResult:
    #: 構造化出力（JSON object）。契約の検証は呼び出し側（handler）が行う
    data: dict[str, Any]
    model: str
    #: 取得できたときだけ。取れなければ None（0 で埋めない）
    input_tokens: int | None = None
    output_tokens: int | None = None


class AgentLlm(Protocol):
    async def generate_json(
        self, *, prompt: str, schema: dict[str, Any], max_tokens: int
    ) -> LlmResult: ...
