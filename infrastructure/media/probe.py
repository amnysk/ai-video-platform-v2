"""``domain.production.media.MediaProbe`` の実装（Pillow / PyAV）。

デコードに失敗したら ``MediaValidationError``（生成揺れとして retryable）。
"""

from __future__ import annotations

import contextlib
import io
from fractions import Fraction
from typing import Any

import av
import av.logging
from av.error import FFmpegError
from PIL import Image, ImageOps, UnidentifiedImageError

from domain.errors import MediaValidationError
from domain.production.media import AudioInfo, ImageInfo, VideoInfo

_PIL_FORMATS = {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp"}

#: Pillow がデコード中に投げうる例外。解凍爆弾（``DecompressionBombError``）は OSError ではない。
PIL_DECODE_ERRORS: tuple[type[BaseException], ...] = (
    UnidentifiedImageError,
    Image.DecompressionBombError,
    OSError,
    ValueError,
    SyntaxError,
    EOFError,
    TypeError,
)


class PillowAvMediaProbe:
    def probe_image(self, data: bytes) -> ImageInfo:
        try:
            with Image.open(io.BytesIO(data)) as image:
                fmt = image.format or ""
                image.load()  # 実際にデコードする（ヘッダだけで通さない）
                # EXIF の向きを反映した見かけの寸法で判定する（正規化と同じ基準）
                width, height = (ImageOps.exif_transpose(image) or image).size
        except PIL_DECODE_ERRORS as exc:
            raise MediaValidationError(f"image is not decodable: {exc}") from exc
        return ImageInfo(format=_PIL_FORMATS.get(fmt, fmt.lower()), width=width, height=height)

    def probe_audio(self, data: bytes) -> AudioInfo:
        try:
            with av.open(io.BytesIO(data), mode="r") as container:
                if not container.streams.audio:
                    raise MediaValidationError("no audio stream")
                stream = container.streams.audio[0]
                samples = 0
                sample_rate = 0
                channels = 0
                for frame in container.decode(stream):
                    samples += frame.samples
                    sample_rate = frame.sample_rate
                    channels = len(frame.layout.channels)
        except MediaValidationError:
            raise
        except (FFmpegError, OSError, ValueError) as exc:
            raise MediaValidationError(f"audio is not decodable: {exc}") from exc
        if sample_rate <= 0 or samples <= 0:
            raise MediaValidationError("audio has no decodable samples")
        return AudioInfo(
            duration_ms=round(samples * 1000 / sample_rate),
            sample_rate_hz=sample_rate,
            channels=channels,
        )

    def probe_video(self, data: bytes) -> VideoInfo:
        """全フレームをデコードし、容器ヘッダの尺とデコードできた尺の両方を返す。

        デコードの例外は ``MediaValidationError``。例外にならずログだけに出たエラーは
        ``decode_errors`` に数え、検査規則（``validate_video``）が不合格にする。
        """
        errors = 0
        try:
            with _capture_ffmpeg_errors() as logs, av.open(io.BytesIO(data), mode="r") as container:
                if not container.streams.video:
                    raise MediaValidationError("no video stream")
                stream = container.streams.video[0]
                has_audio = bool(container.streams.audio)
                rate = stream.average_rate or stream.guessed_rate
                width = stream.codec_context.width
                height = stream.codec_context.height
                frames = 0
                first_pts: int | None = None
                last_pts: int | None = None
                for frame in container.decode(stream):
                    frames += 1
                    if frame.pts is not None:
                        first_pts = frame.pts if first_pts is None else min(first_pts, frame.pts)
                        last_pts = frame.pts if last_pts is None else max(last_pts, frame.pts)
                if stream.duration is not None and stream.time_base is not None:
                    duration_ms = round(float(stream.duration * stream.time_base) * 1000)
                elif container.duration is not None:
                    duration_ms = round(container.duration / 1000)
                else:
                    duration_ms = 0
                time_base = stream.time_base
            errors = _count_errors(logs)
        except MediaValidationError:
            raise
        except (FFmpegError, OSError, ValueError) as exc:
            raise MediaValidationError(f"video is not decodable: {exc}") from exc
        fps = Fraction(rate) if rate else Fraction(0)
        frame_ms = Fraction(1000) / fps if fps > 0 else Fraction(0)
        if first_pts is not None and last_pts is not None and time_base is not None:
            decoded_ms = round(
                Fraction(last_pts - first_pts) * Fraction(time_base) * 1000 + frame_ms
            )
        else:
            decoded_ms = round(frames * frame_ms)
        if duration_ms <= 0:
            duration_ms = decoded_ms
        return VideoInfo(
            duration_ms=duration_ms,
            width=width,
            height=height,
            fps_millis=round(fps * 1000),
            frames_decoded=frames,
            has_audio=has_audio,
            decoded_duration_ms=decoded_ms,
            decode_errors=errors,
        )


def _capture_ffmpeg_errors() -> Any:
    """FFmpeg のログを捕まえる（PyAV に ``av.logging.Capture`` があるときだけ）。"""
    capture = getattr(av.logging, "Capture", None)
    return capture() if capture is not None else contextlib.nullcontext([])


def _count_errors(logs: Any) -> int:
    threshold = getattr(av.logging, "ERROR", 16)
    return sum(1 for entry in (logs or []) if isinstance(entry, tuple) and entry[0] <= threshold)


__all__ = ["PIL_DECODE_ERRORS", "PillowAvMediaProbe"]
