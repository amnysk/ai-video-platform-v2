"""storyboard 生成器の境界（INV-6: domain は純粋。I/O をしない）。

ここには**生成器の実装に依存する語彙を置かない**。どの外部仕様・どの実行手段で
生成するかは ``infrastructure/providers/`` 側だけが知る（ADR-0016）。

生成は段に分ける（ADR-0013 の順序）:

0. ``prepare`` — 入力の検証と局所資源の確保だけ。外部呼び出しをしない（予約より前に呼ぶ）
1. ``generate`` — 有料呼び出しだけを行い、生のテキストを返す
2. ``interpret`` — 生テキストを純粋にパース・検証・変換する
3. ``release`` — 局所資源の解放。例外を投げない

呼び出し側は 1 の結果を保存してから 2 を呼ぶ。2 だけを再実行して回復できるようにするため。
時間軸の正規化（``domain.storyboard.normalize``）は呼び出し側が行う。``interpret`` の責務ではない。
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

    async def prepare(self, request: StoryboardRequest) -> None:
        """入力を検証し、局所資源を確保する。**外部の生成器を呼んではならない。**

        予約（ADR-0013）より前に呼ばれる。ここでの失敗は課金を伴わない。
        失敗は ``domain.errors`` の型（入力不備は needs_input、局所資源の不足は retryable）。
        """
        ...

    async def generate(self, request: StoryboardRequest) -> StoryboardRawResult:
        """有料呼び出しを1回行い、生テキストを返す。``prepare`` 済みであること。"""
        ...

    def interpret(self, raw_text: str, script: ScriptArtifact) -> tuple[StoryboardSceneDraft, ...]:
        """生テキストをパース・検証して下書きへ変換する（純粋）。

        返す下書きは**正規化前**でよい。時間軸の正規化はドメイン規則として呼び出し側が
        ``normalize_timeline`` で一度だけ適用する。
        """
        ...

    async def release(self, request: StoryboardRequest) -> None:
        """``prepare`` で確保した局所資源を解放する。**例外を投げない**（失敗はログに残す）。"""
        ...
