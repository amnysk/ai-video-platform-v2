"""生成メディアの検査規則（ADR-0017）。純粋関数のみ。

デコードは ``MediaProbe`` の実装（infrastructure）が行い、ここは中立な情報だけを見る。
違反は ``MediaValidationError``（retryable: 生成揺れとして扱う）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from contracts.artifacts import MEDIA_MAX_BYTES
from domain.errors import MediaValidationError

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
#: 画像形式の語彙（probe が返す小文字の名前）
ALLOWED_IMAGE_FORMATS: frozenset[str] = frozenset({"png", "jpeg", "webp"})
IMAGE_FORMAT_MIME: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
#: 正規化で切り落としてよい辺の割合の上限。これを超える比率違いは作り直す。
MAX_CROP_FRACTION = 0.25
#: 正規化で許す拡大率の上限。
MAX_UPSCALE = 2.0

VOICE_MIN_DURATION_MS = 200
VOICE_MAX_DURATION_MS = 60_000
VOICE_MIN_SAMPLE_RATE_HZ = 16_000

VIDEO_DURATION_TOLERANCE_MS = 300
VIDEO_DURATION_TOLERANCE_RATIO = 0.05
VIDEO_ASPECT_TOLERANCE = 0.01
VIDEO_MIN_HEIGHT = 1280
VIDEO_MIN_FPS_MILLIS = 20_000
VIDEO_MAX_FPS_MILLIS = 60_000


@dataclass(frozen=True, slots=True)
class ImageInfo:
    format: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class AudioInfo:
    duration_ms: int
    sample_rate_hz: int
    channels: int


@dataclass(frozen=True, slots=True)
class VideoInfo:
    duration_ms: int
    width: int
    height: int
    fps_millis: int
    frames_decoded: int
    has_audio: bool
    #: 実際にデコードできたフレームから求めた尺（最後のフレームの pts + 1フレーム）。
    #: 容器ヘッダの ``duration_ms`` と食い違えば、途中で切れた・壊れたファイルである。
    decoded_duration_ms: int
    #: デコード中に報告されたエラーの数（例外にならず読み飛ばされたものを含む）。
    decode_errors: int = 0


@runtime_checkable
class MediaProbe(Protocol):
    """メディアをデコードして中立な情報を返す。デコードできなければ ``MediaValidationError``。"""

    def probe_image(self, data: bytes) -> ImageInfo: ...

    def probe_audio(self, data: bytes) -> AudioInfo: ...

    def probe_video(self, data: bytes) -> VideoInfo: ...


@dataclass(frozen=True, slots=True)
class NormalizationPlan:
    """元画像から ``crop_box``（left, top, right, bottom）を切り出し、目標解像度へ縮拡する。"""

    crop_box: tuple[int, int, int, int]
    target_width: int
    target_height: int


@dataclass(frozen=True, slots=True)
class NormalizationRejected:
    reason: str


def plan_9x16_normalization(
    width: int,
    height: int,
    tol: float = 0.002,
    *,
    target_width: int = TARGET_WIDTH,
    target_height: int = TARGET_HEIGHT,
) -> NormalizationPlan | NormalizationRejected:
    """任意の解像度を 9:16（既定 1080x1920）へ揃える計画。

    - 比率の差が ``tol`` 以内: 切り出さず縮拡だけ
    - それ以上: 中央から 9:16 を切り出す。ただし辺の ``MAX_CROP_FRACTION`` を超えて
      切り落とす場合、または ``MAX_UPSCALE`` を超えて拡大する場合は拒否する
    """
    if width <= 0 or height <= 0:
        return NormalizationRejected(reason=f"invalid size {width}x{height}")
    target_ratio = target_width / target_height
    ratio = width / height
    if abs(ratio - target_ratio) / target_ratio <= tol:
        box = (0, 0, width, height)
    elif ratio > target_ratio:
        crop_w = round(height * target_ratio)
        if (width - crop_w) / width > MAX_CROP_FRACTION:
            return NormalizationRejected(reason=f"{width}x{height} is too wide for 9:16")
        left = (width - crop_w) // 2
        box = (left, 0, left + crop_w, height)
    else:
        crop_h = round(width / target_ratio)
        if (height - crop_h) / height > MAX_CROP_FRACTION:
            return NormalizationRejected(reason=f"{width}x{height} is too tall for 9:16")
        top = (height - crop_h) // 2
        box = (0, top, width, top + crop_h)
    crop_height = box[3] - box[1]
    if target_height / crop_height > MAX_UPSCALE:
        return NormalizationRejected(
            reason=f"{width}x{height} would need more than {MAX_UPSCALE}x upscale"
        )
    return NormalizationPlan(crop_box=box, target_width=target_width, target_height=target_height)


def validate_image(info: ImageInfo, size_bytes: int) -> None:
    """正規化**後**の画像を検査する。"""
    if info.format not in ALLOWED_IMAGE_FORMATS:
        raise MediaValidationError(f"image format {info.format!r} not allowed")
    if (info.width, info.height) != (TARGET_WIDTH, TARGET_HEIGHT):
        raise MediaValidationError(
            f"image must be {TARGET_WIDTH}x{TARGET_HEIGHT}, got {info.width}x{info.height}"
        )
    _validate_size(size_bytes)


def validate_voice(info: AudioInfo, size_bytes: int) -> None:
    if not VOICE_MIN_DURATION_MS <= info.duration_ms <= VOICE_MAX_DURATION_MS:
        raise MediaValidationError(
            f"voice duration {info.duration_ms} ms outside "
            f"{VOICE_MIN_DURATION_MS}..{VOICE_MAX_DURATION_MS}"
        )
    if info.sample_rate_hz < VOICE_MIN_SAMPLE_RATE_HZ:
        raise MediaValidationError(f"voice sample rate {info.sample_rate_hz} too low")
    if not 1 <= info.channels <= 2:
        raise MediaValidationError(f"voice channels {info.channels} not in 1..2")
    _validate_size(size_bytes)


def video_duration_tolerance_ms(requested_ms: int) -> int:
    """尺の許容差: max(300ms, 要求尺の5%)。provider は尺を丸めることがある。"""
    return max(VIDEO_DURATION_TOLERANCE_MS, round(requested_ms * VIDEO_DURATION_TOLERANCE_RATIO))


def validate_video(info: VideoInfo, size_bytes: int, *, requested_duration_ms: int) -> None:
    if info.frames_decoded <= 0:
        raise MediaValidationError("video has no decodable frames")
    if info.decode_errors > 0:
        raise MediaValidationError(f"video decode reported {info.decode_errors} error(s)")
    decoded_tolerance = video_duration_tolerance_ms(info.duration_ms)
    if abs(info.decoded_duration_ms - info.duration_ms) > decoded_tolerance:
        raise MediaValidationError(
            f"decoded video duration {info.decoded_duration_ms} ms differs from container "
            f"duration {info.duration_ms} ms by more than {decoded_tolerance} ms"
        )
    tolerance = video_duration_tolerance_ms(requested_duration_ms)
    if abs(info.duration_ms - requested_duration_ms) > tolerance:
        raise MediaValidationError(
            f"video duration {info.duration_ms} ms differs from requested "
            f"{requested_duration_ms} ms by more than {tolerance} ms"
        )
    if info.width <= 0 or info.height <= 0:
        raise MediaValidationError(f"invalid video size {info.width}x{info.height}")
    target = 9 / 16
    if abs(info.width / info.height - target) / target > VIDEO_ASPECT_TOLERANCE:
        raise MediaValidationError(f"video {info.width}x{info.height} is not 9:16")
    if info.height < VIDEO_MIN_HEIGHT:
        raise MediaValidationError(f"video height {info.height} < {VIDEO_MIN_HEIGHT}")
    if not VIDEO_MIN_FPS_MILLIS <= info.fps_millis <= VIDEO_MAX_FPS_MILLIS:
        raise MediaValidationError(f"video fps {info.fps_millis / 1000} outside 20..60")
    _validate_size(size_bytes)


def _validate_size(size_bytes: int) -> None:
    if not 0 < size_bytes < MEDIA_MAX_BYTES:
        raise MediaValidationError(f"media size {size_bytes} bytes outside (0, {MEDIA_MAX_BYTES})")
