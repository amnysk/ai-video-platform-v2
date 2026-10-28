"""render stage の port（Phase 5）。実装は infrastructure にある。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from contracts.music_render import BgmMixPlan, LoudnessMeasurement
from contracts.render import RenderEngineIdentity, RenderPlan


@dataclass(frozen=True, slots=True)
class FinalVideoInfo:
    """完成動画をデコードして測った中立な情報。技術 QA はこれだけを見る。"""

    duration_ms: int
    width: int
    height: int
    fps_millis: int
    frames_decoded: int
    #: デコード中に報告されたエラーの数（例外にならず読み飛ばされたものを含む）
    decode_errors: int
    video_codec: str
    pix_fmt: str
    audio_present: bool
    #: 音声が無ければ ``None``
    audio_codec: str | None
    audio_sample_rate_hz: int | None
    audio_channels: int | None
    #: デコードできた音声サンプルから求めた尺。音声が無ければ ``None``
    audio_duration_ms: int | None
    bytes: int


@runtime_checkable
class FinalVideoProbe(Protocol):
    """ファイルを先頭から流しながらデコードする（全体をメモリに載せない）。

    デコードできなければ ``MediaValidationError``。
    """

    def probe_final_video(self, path: str) -> FinalVideoInfo: ...


@dataclass(frozen=True, slots=True)
class BgmRenderInput:
    """BGM を混ぜるときだけ渡す。音源は sha256 照合済みのローカルファイル（track_id → パス）。"""

    mix: BgmMixPlan
    track_paths: Mapping[str, Path]


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """描画1回分の入力。素材は作業領域へ取り出して sha256 を照合済みのローカルファイル。"""

    plan: RenderPlan
    #: storyboard scene_id -> シーン動画
    scene_video_paths: Mapping[str, Path]
    #: script_scene_id -> ナレーション音声
    voice_paths: Mapping[str, Path]
    #: ``plan.subtitle_cues`` と同じ順・同じ数の表示文字列（台本ナレーションから具体化したもの）
    subtitle_texts: Sequence[str]
    font_path: Path
    #: エンジンが中間ファイル・ログを置く場所（JobWorkDir の内側）
    work_dir: Path
    output_path: Path
    timeout_seconds: float
    #: None なら BGM 無し（導入前と同一の描画）
    bgm: BgmRenderInput | None = None


@dataclass(frozen=True, slots=True)
class RenderedVideo:
    path: Path
    bytes: int


@runtime_checkable
class RenderEngine(Protocol):
    """計画どおりに完成動画を1本描く。

    cancel は ``asyncio.CancelledError`` をそのまま伝える
    （実装は子プロセスを止めてから再送出する）。
    失敗は ``RenderEngineFailedError`` / ``RenderEngineTimeoutError`` /
    ``RenderWorkspaceFullError`` / ``RenderEngineUnavailableError``。
    """

    def identity(self) -> RenderEngineIdentity: ...

    async def render(
        self, request: RenderRequest, *, heartbeat: Callable[[], object]
    ) -> RenderedVideo: ...


@runtime_checkable
class LoudnessProbe(Protocol):
    """完成動画の音声のラウドネスを実測する（同期。呼び出し側がスレッドで待つ）。

    実測できなければ ``RenderEngineFailedError``（再試行可能）。値を推測して返さない。
    """

    def measure_loudness(self, path: str) -> LoudnessMeasurement: ...
