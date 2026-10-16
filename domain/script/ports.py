"""台本生成器の境界（INV-6: domain は純粋。I/O をしない）。

ここには **生成器の実装に依存する語彙を置かない**。将来 Codex CLI から
OpenAI API / Claude / ローカルモデルへ差し替えるとき、書き換えるのは
``infrastructure/providers/`` 側だけで済むようにするための境界である。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """1回の生成依頼。

    ``output_schema`` は生成器に守らせたい JSON Schema（不要なら ``None``）。
    ``timeout_seconds`` は呼び出し側の待ち時間の上限で、生成器はこれを超えたら
    ``ProviderTimeoutError`` を送出する。
    """

    episode_id: str
    prompt: str
    output_schema: dict[str, Any] | None
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """生成器が返したもの。**パースも検証もここではしない。**

    ``raw_log`` は監査用の実行ログで、secret を含めてはならない（AGENTS.md §9）。
    """

    text: str
    provider_id: str
    model: str
    raw_log: str


@runtime_checkable
class StoryGenerator(Protocol):
    """台本のもとになるテキストを生成するもの。

    失敗は ``domain.errors`` の ``Provider*Error`` で表現する。
    """

    async def generate(self, request: GenerationRequest) -> GenerationResult: ...
