"""Agent stage が gateway に渡す LLM の包み（ADR-0043 §2/§6）。

- ``RecordingLlm``: 実際に返ってきた応答（と試みた回数）を残す。応答は予約台帳の evidence になり、
  成功した呼び出しの prompt・schema・使用量・応答を ``agent_runs`` へ渡す
- ``ReplayLlm``: 前の試行が呼び終えていたとき（予約に ``raw_output_key`` がある）、
  **LLM を呼び直さず**保存済みの応答を同じ順序で返す。
  足りなければ ``ProviderInvocationError``（課金済みの呼び出しを黙って増やさない）
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from domain.agents.ports import AgentLlm, LlmResult
from domain.errors import ProviderInvocationError
from infrastructure.agents.persist import LlmCallRecord


class RecordingLlm:
    def __init__(self, inner: AgentLlm) -> None:
        self._inner = inner
        self.calls: list[LlmCallRecord] = []
        #: 呼び出しを始めた回数（失敗した呼び出しを含む）
        self.attempted = 0

    async def generate_json(
        self, *, prompt: str, schema: dict[str, Any], max_tokens: int
    ) -> LlmResult:
        self.attempted += 1
        result = await self._inner.generate_json(
            prompt=prompt, schema=schema, max_tokens=max_tokens
        )
        self.calls.append(
            LlmCallRecord(
                prompt=prompt,
                schema=schema,
                data=result.data,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        )
        return result

    def raw_document(self) -> dict[str, Any]:
        """予約台帳の evidence（呼んだ事実と応答）。"""
        return {"calls": [c.raw() for c in self.calls], "attempted": self.attempted}


class ReplayLlm:
    def __init__(self, recorded: Sequence[dict[str, Any]]) -> None:
        self._recorded = list(recorded)
        self._next = 0

    async def generate_json(
        self, *, prompt: str, schema: dict[str, Any], max_tokens: int
    ) -> LlmResult:
        if self._next >= len(self._recorded):
            raise ProviderInvocationError(
                "the recorded agent LLM responses are exhausted; refusing to call the LLM again"
            )
        item = self._recorded[self._next]
        self._next += 1
        return LlmResult(
            data=dict(item["data"]),
            model=str(item["model"]),
            input_tokens=item.get("input_tokens"),
            output_tokens=item.get("output_tokens"),
        )
