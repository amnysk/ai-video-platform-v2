"""render stage の port（Phase 5）。実装は infrastructure にある。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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
