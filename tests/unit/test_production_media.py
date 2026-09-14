"""メディア検査規則・9:16 正規化計画・実デコード（ADR-0017）。"""

from __future__ import annotations

import pytest

from domain.errors import MediaValidationError
from domain.production.media import (
    MAX_CROP_FRACTION,
    AudioInfo,
    ImageInfo,
    MediaProbe,
    NormalizationPlan,
    NormalizationRejected,
    VideoInfo,
    plan_9x16_normalization,
    validate_image,
    validate_video,
    validate_voice,
    video_duration_tolerance_ms,
)
from infrastructure.media.normalize import normalize_image_9x16
from infrastructure.media.probe import PillowAvMediaProbe
from tests.support.production import make_mp4, make_png, make_wav

MB = 1024 * 1024

# --------------------------------------------------------------------------- 正規化計画


def test_exact_9x16_needs_no_crop() -> None:
    plan = plan_9x16_normalization(1080, 1920)
    assert plan == NormalizationPlan(
        crop_box=(0, 0, 1080, 1920), target_width=1080, target_height=1920
    )


def test_within_tolerance_scales_without_crop() -> None:
    plan = plan_9x16_normalization(1024, 1820)  # 0.5626 vs 0.5625
    assert isinstance(plan, NormalizationPlan)
    assert plan.crop_box == (0, 0, 1024, 1820)


def test_wider_image_is_center_cropped() -> None:
    plan = plan_9x16_normalization(1200, 1920)
    assert isinstance(plan, NormalizationPlan)
    assert plan.crop_box == (60, 0, 1140, 1920)


def test_taller_image_is_center_cropped() -> None:
    plan = plan_9x16_normalization(1080, 2100)
    assert isinstance(plan, NormalizationPlan)
    assert plan.crop_box == (0, 90, 1080, 2010)


def test_landscape_is_rejected() -> None:
    assert isinstance(plan_9x16_normalization(1920, 1080), NormalizationRejected)


def test_crop_fraction_limit() -> None:
    width = round(1920 * 9 / 16 / (1 - MAX_CROP_FRACTION)) + 20
    assert isinstance(plan_9x16_normalization(width, 1920), NormalizationRejected)


def test_excessive_upscale_is_rejected() -> None:
    assert isinstance(plan_9x16_normalization(360, 640), NormalizationRejected)
    assert isinstance(plan_9x16_normalization(540, 960), NormalizationPlan)


@pytest.mark.parametrize(("w", "h"), [(0, 1920), (1080, 0), (-1, 5)])
def test_invalid_sizes_are_rejected(w: int, h: int) -> None:
    assert isinstance(plan_9x16_normalization(w, h), NormalizationRejected)


# --------------------------------------------------------------------------- 検査規則


def test_image_rules() -> None:
    validate_image(ImageInfo("png", 1080, 1920), 10)
    with pytest.raises(MediaValidationError):
        validate_image(ImageInfo("gif", 1080, 1920), 10)
    with pytest.raises(MediaValidationError):
        validate_image(ImageInfo("png", 1080, 1921), 10)
    with pytest.raises(MediaValidationError):
        validate_image(ImageInfo("png", 1080, 1920), 0)
    with pytest.raises(MediaValidationError):
        validate_image(ImageInfo("png", 1080, 1920), 25 * MB)


@pytest.mark.parametrize(
    ("info", "ok"),
    [
        (AudioInfo(200, 16000, 1), True),
        (AudioInfo(60000, 48000, 2), True),
        (AudioInfo(199, 16000, 1), False),
        (AudioInfo(60001, 16000, 1), False),
        (AudioInfo(1000, 15999, 1), False),
        (AudioInfo(1000, 16000, 3), False),
        (AudioInfo(1000, 16000, 0), False),
    ],
)
def test_voice_rules(info: AudioInfo, ok: bool) -> None:
    if ok:
        validate_voice(info, 100)
    else:
        with pytest.raises(MediaValidationError):
            validate_voice(info, 100)


def _video(**overrides) -> VideoInfo:
    base = {
        "duration_ms": 5000,
        "width": 720,
        "height": 1280,
        "fps_millis": 24000,
        "frames_decoded": 120,
        "decoded_duration_ms": 5000,
        "has_audio": False,
    }
    return VideoInfo(**{**base, **overrides})


def test_video_duration_tolerance_is_max_of_300ms_and_5_percent() -> None:
    assert video_duration_tolerance_ms(4000) == 300
    assert video_duration_tolerance_ms(10000) == 500


@pytest.mark.parametrize(
    ("overrides", "ok"),
    [
        ({}, True),
        ({"duration_ms": 5250}, True),
        ({"duration_ms": 5301}, False),
        ({"frames_decoded": 0}, False),
        ({"width": 1280, "height": 720}, False),
        ({"width": 736, "height": 1280}, False),  # 1% 超の比率ずれ
        ({"width": 1088, "height": 1920}, True),  # 0.7% のずれは許容
        ({"width": 540, "height": 960}, False),
        ({"fps_millis": 19999}, False),
        ({"fps_millis": 60001}, False),
        ({"has_audio": True}, True),
        ({"decoded_duration_ms": 4700}, True),  # 容器尺との差 300ms 以内
        ({"decoded_duration_ms": 3000}, False),  # 容器は 5s と言うがデコードできたのは 3s
        ({"decode_errors": 1}, False),
    ],
)
def test_video_rules(overrides: dict, ok: bool) -> None:
    info = _video(**overrides)
    if ok:
        validate_video(info, 1000, requested_duration_ms=5000)
    else:
        with pytest.raises(MediaValidationError):
            validate_video(info, 1000, requested_duration_ms=5000)


# --------------------------------------------------------------------------- 実デコード


def test_probe_implements_the_domain_port() -> None:
    assert isinstance(PillowAvMediaProbe(), MediaProbe)


def test_probe_real_png() -> None:
    info = PillowAvMediaProbe().probe_image(make_png(1080, 1920))
    assert info == ImageInfo("png", 1080, 1920)


def test_probe_real_wav() -> None:
    info = PillowAvMediaProbe().probe_audio(make_wav(1500, 22050, 1))
    assert info.sample_rate_hz == 22050 and info.channels == 1
    assert abs(info.duration_ms - 1500) <= 5
    validate_voice(info, 10)


def test_probe_real_mp4() -> None:
    data = make_mp4(1000, 720, 1280, 24)
    info = PillowAvMediaProbe().probe_video(data)
    assert (info.width, info.height, info.fps_millis) == (720, 1280, 24000)
    assert info.frames_decoded == 24 and not info.has_audio
    validate_video(info, len(data), requested_duration_ms=1000)


def test_probe_reports_decoded_duration() -> None:
    info = PillowAvMediaProbe().probe_video(make_mp4(2000, 720, 1280, 24, faststart=True))
    assert abs(info.decoded_duration_ms - 2000) <= 50 and info.decode_errors == 0


@pytest.mark.parametrize("fraction", [0.9, 0.6, 0.3])
def test_truncated_mp4_is_rejected(fraction: float) -> None:
    """moov が残ったまま mdat が欠けた mp4 は、容器の尺を信じず不合格にする。"""
    data = make_mp4(2000, 720, 1280, 24, faststart=True)
    cut = data[: int(len(data) * fraction)]
    probe = PillowAvMediaProbe()
    with pytest.raises(MediaValidationError):
        info = probe.probe_video(cut)
        validate_video(info, len(cut), requested_duration_ms=2000)


@pytest.mark.parametrize("method", ["probe_image", "probe_audio", "probe_video"])
def test_probe_rejects_garbage(method: str) -> None:
    with pytest.raises(MediaValidationError):
        getattr(PillowAvMediaProbe(), method)(b"not media at all")


def test_normalizer_produces_validated_1080x1920_png_deterministically() -> None:
    source = make_png(1200, 2000)
    first = normalize_image_9x16(source)
    assert (first.width, first.height, first.mime) == (1080, 1920, "image/png")
    assert normalize_image_9x16(source).data == first.data
    info = PillowAvMediaProbe().probe_image(first.data)
    validate_image(info, len(first.data))


def test_normalizer_rejects_landscape() -> None:
    with pytest.raises(MediaValidationError):
        normalize_image_9x16(make_png(1920, 1080))
