"""render stage の port（Phase 5）。実装は infrastructure にある。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from contracts.artifacts import ScriptArtifact, StoryboardArtifact
from contracts.music import BgmTrack, MusicCueSheet
from contracts.music_render import BgmMixPlan, LoudnessMeasurement
from contracts.music_selection import BgmSelectionDecision, FallbackReason
from contracts.render import RenderEngineIdentity, RenderPlan
from domain.music.intelligibility import SceneSpeechLevels


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


@runtime_checkable
class SpeechMarginProbe(Protocol):
    """発話中の声と BGM のラウドネスを scene ごとに実測する（同期。ADR-0083 §5 / ADR-0086 §3）。

    実測できなければ ``RenderEngineFailedError``。値を推測して返さない。
    """

    def measure(
        self, plan: RenderPlan, bgm: BgmRenderInput, voice_paths: Mapping[str, Path]
    ) -> tuple[SceneSpeechLevels, ...]: ...


class BgmSourceError(Exception):
    """BGM の供給元（カタログ・cue sheet・音源）が読めない/壊れている。BGM を使わず描画を続ける。"""


@dataclass(frozen=True, slots=True)
class BgmSourceData:
    """Episode 1 件分の BGM 入力。cue sheet が無ければ ``cue_sheet=None``（BGM 無し）。"""

    cue_sheet: MusicCueSheet | None
    catalog: tuple[BgmTrack, ...] = ()
    #: track_id → ローカルの音源ファイル
    track_paths: Mapping[str, Path] = field(default_factory=dict)
    #: track_id → 実ファイルから求めた sha256（読めなければ None）
    file_sha256_by_track: Mapping[str, str | None] = field(default_factory=dict)


@runtime_checkable
class BgmSource(Protocol):
    """BGM の供給元。同期（呼び出し側がスレッドで待つ）。DB 版への差し替え口（ADR-0077 §1）。"""

    def load(self, episode_id: str) -> BgmSourceData: ...


@dataclass(frozen=True, slots=True)
class BgmRenderContext:
    """選曲に必要な、Render が持っている文脈（ADR-0087 §5）。Activity の入出力には現れない。"""

    episode_id: str
    plan: RenderPlan
    script: ScriptArtifact
    storyboard: StoryboardArtifact
    #: 原本を取り出す作業領域（sha256 照合済みのキャッシュ置き場）
    work_dir: Path
    now: datetime


@dataclass(frozen=True, slots=True)
class BgmRenderSelection:
    """``load_for_render`` の結果。``finalized`` なら梯子は走らせず、``data`` をそのまま使う。"""

    decision: BgmSelectionDecision
    finalized: bool
    #: 確定済みで曲があるときだけ。確定済みの cue sheet と取り出し済みの原本
    data: BgmSourceData | None = None
    #: 未確定のとき、梯子で試す曲（最上位から）
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BgmCandidate:
    """梯子の 1 段分の準備結果。``failure`` があれば ``data`` は無く、次の曲へ進む。"""

    data: BgmSourceData | None = None
    failure: FallbackReason | None = None
    #: 曲の ``mix_gain_offset_db`` に追加の減衰を足した、実際に適用する値
    applied_gain_offset_db: int = 0


@runtime_checkable
class SelectingBgmSource(Protocol):
    """Episode 単位の自動選曲つきの供給元（DB 版。ADR-0087 §5）。

    旧 ``BgmSource.load`` と並ぶ別の口で、``LocalDirectoryBgmSource`` は変わらない。
    ネットワークで収集しない・待たない（需要を積むだけ）。
    """

    async def load_for_render(self, ctx: BgmRenderContext) -> BgmRenderSelection: ...

    async def prepare_candidate(
        self, ctx: BgmRenderContext, track_id: str, *, extra_gain_offset_db: int
    ) -> BgmCandidate: ...

    async def finalize(
        self, decision: BgmSelectionDecision, *, finalized_at: datetime
    ) -> BgmSelectionDecision: ...
