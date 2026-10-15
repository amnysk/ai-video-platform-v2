"""決定的な偽 LLM（ADR-0043 §2）。単体テストと隔離 E2E スタック（``JPMAS2_AGENT_LLM=fake``）用。

外部へは何も出ない。token 数は文字数から決定的に決める（4 文字 = 1 token、切り上げ）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from domain.agents.ports import LlmResult

FAKE_MODEL = "fake-deterministic"

Responder = Callable[[str, dict[str, Any]], dict[str, Any]]


def _tokens(text: str) -> int:
    return (len(text) + 3) // 4


class FakeAgentLlm:
    """``responses`` は「prompt に含まれる文字列 -> 返す JSON」の表、または
    ``(prompt, schema) -> dict``。

    表は挿入順で最初に一致したものを返す。どれにも一致しなければ ``LookupError``
    （黙って空の応答を返さない）。
    """

    def __init__(self, responses: Mapping[str, dict[str, Any]] | Responder) -> None:
        self._responses = responses
        #: 受け取った (prompt, schema, max_tokens)。テストが検査する
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    async def generate_json(
        self, *, prompt: str, schema: dict[str, Any], max_tokens: int
    ) -> LlmResult:
        self.calls.append((prompt, schema, max_tokens))
        if callable(self._responses):
            data = self._responses(prompt, schema)
        else:
            for needle, candidate in self._responses.items():
                if needle in prompt:
                    data = candidate
                    break
            else:
                raise LookupError("FakeAgentLlm has no response for this prompt")
        output = json.dumps(data, ensure_ascii=False, sort_keys=True)
        return LlmResult(
            data=dict(data),
            model=FAKE_MODEL,
            input_tokens=_tokens(prompt),
            output_tokens=_tokens(output),
        )
