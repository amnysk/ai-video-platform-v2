"""storyboard 生成器の境界（INV-6: domain は純粋。I/O をしない）。

ここには**生成器の実装に依存する語彙を置かない**。どの外部仕様・どの実行手段で
生成するかは ``infrastructure/providers/`` 側だけが知る（ADR-0016）。

生成は2段に分ける（ADR-0013 の順序）:

1. ``generate`` — 有料呼び出しだけを行い、生のテキストを返す
2. ``interpret`` — 生テキストを純粋にパース・検証・変換する

呼び出し側は 1 の結果を保存してから 2 を呼ぶ。2 だけを再実行して回復できるようにするため。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from contracts.artifacts import ScriptArtifact, StoryboardVisualKind


@dataclass(frozen=True, slots=True)
class StoryboardRequest:
    """1回の storyboard 生成依頼。``timeout_seconds`` を超えたら ``ProviderTimeoutError``。"""

    episode_id: str
    job_id: str
    script: ScriptArtifact
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class StoryboardRawResult:
    """生成器が返したもの。パースも検証もしていない。``raw_log`` に secret を含めない。"""

    text: str
    provider_id: str
    model: str
    raw_log: str


@dataclass(frozen=True, slots=True)
class StoryboardSceneDraft:
    """解釈済みのシーン下書き。``scene_id`` / ``order`` はまだ持たない（システムが採番する）。"""

    script_scene_id: str
    start_ms: int
    duration_ms: int
    visual_kind: StoryboardVisualKind
    visual_description: str
    framing: str | None = None
    camera_movement: str | None = None
    transition_in: str | None = None


@runtime_checkable
class StoryboardGenerator(Protocol):
    """台本から storyboard を生成するもの。

    - ``generator_id``: 生成器の同一性（実装種別 + モデル）。``input_hash`` に入る
    - ``generation_spec_id``: 生成を誘導する仕様の内容由来の同一性。``input_hash`` に入る
    - 失敗は ``domain.errors`` の例外型で表現する
    """

    @property
    def generator_id(self) -> str: ...

    @property
    def generation_spec_id(self) -> str: ...

    async def generate(self, request: StoryboardRequest) -> StoryboardRawResult: ...

    def interpret(
        self, raw_text: str, script: ScriptArtifact
    ) -> tuple[StoryboardSceneDraft, ...]: ...
