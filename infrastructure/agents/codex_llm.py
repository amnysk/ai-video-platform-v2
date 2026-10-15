"""Codex CLI を ``AgentLlm`` として使うアダプタ（ADR-0043 §2）。

Codex 固有の呼び出し（argv・sandbox・JSONL）は既存の ``CodexCliStoryGenerator`` が持つので、
ここではそれを ``StoryGenerator`` port 越しに受け取る（``infrastructure.providers.codex_cli`` を
直接 import しない。import 元は INV-18 の architecture test が数えている）。

- JSON Schema は ``output_schema`` として渡し、最終メッセージを JSON object として読む
- token 使用量は JSONL の ``turn.completed`` の ``usage`` が**読めたときだけ**入れる。
  読めなければ None
  （0 で埋めない。Codex の policy には単価も無く、実績費用は測れない）
- ``max_tokens`` は Codex CLI に渡す手段が無いので強制できない（予算は予約の上限で数える）
"""

from __future__ import annotations

import json
import re
from typing import Any

from domain.agents.ports import LlmResult
from domain.errors import ProviderInvocationError
from domain.script.ports import GenerationRequest, StoryGenerator

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_json_object(text: str) -> dict[str, Any]:
    body = text.strip()
    fenced = _FENCE_RE.match(body)
    if fenced:
        body = fenced.group(1)
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderInvocationError("agent LLM output is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProviderInvocationError("agent LLM output is not a JSON object")
    return value


def parse_token_usage(raw_log: str) -> tuple[int | None, int | None]:
    """JSONL の ``turn.completed`` から (input, output)。読めない側は None。"""
    for line in raw_log.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return None, None
        return _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
    return None, None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class CodexAgentLlm:
    def __init__(self, *, generator: StoryGenerator, episode_id: str, timeout_seconds: int) -> None:
        self._generator = generator
        self._episode_id = episode_id
        self._timeout_seconds = timeout_seconds

    async def generate_json(
        self, *, prompt: str, schema: dict[str, Any], max_tokens: int
    ) -> LlmResult:
        result = await self._generator.generate(
            GenerationRequest(
                episode_id=self._episode_id,
                prompt=prompt,
                output_schema=schema,
                timeout_seconds=self._timeout_seconds,
            )
        )
        input_tokens, output_tokens = parse_token_usage(result.raw_log)
        return LlmResult(
            data=parse_json_object(result.text),
            model=result.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
