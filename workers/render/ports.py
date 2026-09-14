"""描画 Activity が注入で受け取る協力者の形（ADR-0019 / INV-18）。

Activity は計画の組み立て・同一性・技術検査（domain/render の純粋関数）と描画エンジン
（infrastructure/render）を**ここの Protocol 越しにだけ**使う。worker の組み立て（run_worker）が
本物を渡し、テストは小さな fake を渡す。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from contracts.artifacts import (
    ProductionManifest,
    SceneVideoArtifact,
    SceneVoiceArtifact,
    ScriptArtifact,
    StoryboardArtifact,
)
from contracts.render import (
    RenderEngineIdentity,
    RenderPlan,
    RenderProfile,
    TechnicalQaReport,
    TimelinePolicy,
)
from domain.render.ports import FinalVideoInfo

#: 引数なしで呼ぶ heartbeat（Activity の外では何もしない）。
Heartbeat = Callable[[], None]


@dataclass(frozen=True, slots=True)
class RenderInputs:
    """PostgreSQL の現行メタデータと MinIO の本体を突き合わせ、契約で読んだ入力一式。"""

    episode_id: str
    manifest: ProductionManifest
    manifest_artifact_id: str
    manifest_sha256: str
    script: ScriptArtifact
    script_artifact_id: str
    script_sha256: str
    storyboard: StoryboardArtifact
    storyboard_artifact_id: str
    storyboard_sha256: str
    #: storyboard scene_id -> シーン動画 Artifact
    videos: Mapping[str, SceneVideoArtifact]
    #: script scene_id -> 音声 Artifact
    voices: Mapping[str, SceneVoiceArtifact]


class RenderPlanning(Protocol):
    """domain/render の純粋関数の束。失敗は domain の例外で表す。"""

    def build_plan(
        self,
        inputs: RenderInputs,
        *,
        profile: RenderProfile,
        policy: TimelinePolicy,
        engine: RenderEngineIdentity,
    ) -> RenderPlan: ...

    def plan_sha256(self, plan: RenderPlan) -> str: ...

    def input_hash(
        self,
        *,
        manifest_sha256: str,
        script_sha256: str,
        storyboard_sha256: str,
        profile: RenderProfile,
        policy: TimelinePolicy,
        engine: RenderEngineIdentity,
        font_sha256: str,
    ) -> str: ...

    def subtitle_texts(self, plan: RenderPlan, script: ScriptArtifact) -> Sequence[str]:
        """``plan.subtitle_cues`` と同じ順の表示文字列（台本ナレーションから具体化する）。"""
        ...

    def technical_qa(
        self,
        *,
        plan: RenderPlan,
        info: FinalVideoInfo,
        media_sha256: str,
        media_readback_sha256: str,
    ) -> TechnicalQaReport:
        """合格なら報告を返す。不合格は FinalVideoValidationError か FinalVideoCorruptError。"""
        ...

    def final_media_key(self, episode_id: str, sha256: str) -> str:
        """完成動画本体の Episode 単位のキー（``domain.artifact.keys``）。"""
        ...


@dataclass(frozen=True, slots=True)
class RenderJob:
    """エンジンへ渡す1回分の描画。パスはすべて作業領域の中。"""

    plan: RenderPlan
    #: storyboard scene_id -> 読み戻し検証済みのシーン動画
    scene_video_paths: Mapping[str, Path]
    #: script scene_id -> 読み戻し検証済みの音声
    voice_paths: Mapping[str, Path]
    subtitle_texts: Sequence[str]
    font_path: Path
    work_dir: Path
    output_path: Path
    timeout_seconds: int


class FinalVideoRenderer(Protocol):
    """描画エンジン。cancel（``asyncio.CancelledError``）では子プロセスを止めてから再送出する。"""

    def identity(self) -> RenderEngineIdentity: ...

    async def render(self, job: RenderJob, heartbeat: Heartbeat) -> Path:
        """``job.output_path`` に書いてそのパスを返す。

        非zero終了は ``RenderEngineFailedError``、時間切れは ``RenderEngineTimeoutError``。
        """
        ...


__all__ = [
    "FinalVideoRenderer",
    "Heartbeat",
    "RenderInputs",
    "RenderJob",
    "RenderPlanning",
]
