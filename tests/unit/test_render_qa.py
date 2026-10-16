"""完成動画の技術検査（ADR-0019 §6）と Episode 単位のメディアキー。"""

from __future__ import annotations

from typing import Any

import pytest

from contracts.render import FINAL_VIDEO_MAX_BYTES
from domain.artifact.keys import episode_media_object_key, media_object_key
from domain.errors import (
    FinalVideoCorruptError,
    FinalVideoValidationError,
    RenderInputIntegrityError,
)
from domain.render.qa import (
    TECHNICAL_QA_CHECK_NAMES,
    evaluate_technical_qa,
    measured_from_info,
    run_technical_qa,
)
from tests.support.render_fixtures import good_final_video_info, render_inputs

INPUTS = render_inputs()
PLAN = INPUTS.plan()
PROFILE = PLAN.profile


def _run(info_over: dict[str, Any] | None = None, **kwargs: Any):
    info = good_final_video_info(PLAN, **(info_over or {}))
    options: dict[str, Any] = {
        "media_bytes": info.bytes,
        "readback_sha_ok": True,
        "sources_verified": True,
        "manifest": INPUTS.manifest,
    }
    options.update(kwargs)
    return run_technical_qa(PLAN, PROFILE, info, **options)


def test_good_video_passes_every_named_check() -> None:
    report = _run()
    assert report.passed is True
    assert tuple(c.check for c in report.checks) == TECHNICAL_QA_CHECK_NAMES


@pytest.mark.parametrize(
    ("over", "kwargs", "error"),
    [
        ({"frames_decoded": 0}, {}, FinalVideoCorruptError),
        ({"decode_errors": 3}, {}, FinalVideoCorruptError),
        ({}, {"readback_sha_ok": False}, FinalVideoCorruptError),
        ({}, {"sources_verified": False}, RenderInputIntegrityError),
        ({"width": 1920, "height": 1080}, {}, FinalVideoValidationError),
        ({"width": 1080, "height": 1080}, {}, FinalVideoValidationError),
        ({"fps_millis": 29_970}, {}, FinalVideoValidationError),
        ({"duration_ms": 25_200}, {}, FinalVideoValidationError),
        ({"video_codec": "hevc"}, {}, FinalVideoValidationError),
        ({"pix_fmt": "yuv444p"}, {}, FinalVideoValidationError),
        (
            {
                "audio_present": False,
                "audio_codec": None,
                "audio_sample_rate_hz": None,
                "audio_channels": None,
                "audio_duration_ms": None,
            },
            {},
            FinalVideoValidationError,
        ),
        ({"audio_codec": "mp3"}, {}, FinalVideoValidationError),
        ({"audio_sample_rate_hz": 44_100}, {}, FinalVideoValidationError),
        ({"audio_channels": 1}, {}, FinalVideoValidationError),
        ({"audio_duration_ms": 20_000}, {}, FinalVideoValidationError),
        ({}, {"media_bytes": 999}, FinalVideoValidationError),
        (
            {"bytes": FINAL_VIDEO_MAX_BYTES + 1},
            {"media_bytes": FINAL_VIDEO_MAX_BYTES + 1},
            FinalVideoValidationError,
        ),
    ],
)
def test_failures_map_to_error_classes(
    over: dict[str, Any], kwargs: dict[str, Any], error: type[Exception]
) -> None:
    with pytest.raises(error):
        _run(over, **kwargs)


def test_tolerances() -> None:
    _run({"fps_millis": PROFILE.fps_millis + 1})
    _run({"duration_ms": PLAN.total_duration_ms + PROFILE.limits.duration_tolerance_ms})


def test_corrupt_wins_over_validation() -> None:
    with pytest.raises(FinalVideoCorruptError):
        _run({"frames_decoded": 0, "width": 2})


def test_profile_limits_are_checked_against_the_plan() -> None:
    short = PROFILE.model_copy(
        update={"limits": PROFILE.limits.model_copy(update={"max_duration_ms": 20_000})}
    )
    info = good_final_video_info(PLAN)
    with pytest.raises(FinalVideoValidationError):
        run_technical_qa(
            PLAN, short, info, media_bytes=info.bytes, readback_sha_ok=True, sources_verified=True
        )


def test_manifest_scene_mismatch_fails_coverage() -> None:
    other = render_inputs()
    manifest = other.manifest.model_copy(update={"scenes": other.manifest.scenes[:3]})
    checks = evaluate_technical_qa(
        PLAN,
        PROFILE,
        good_final_video_info(PLAN),
        media_bytes=123_456,
        readback_sha_ok=True,
        sources_verified=True,
        manifest=manifest,
    )
    failed = {c.check for c in checks if not c.passed}
    assert failed == {"scene_coverage"}


def test_audio_optional_profile_passes_without_audio() -> None:
    profile = PROFILE.model_copy(
        update={"limits": PROFILE.limits.model_copy(update={"audio_required": False})}
    )
    plan = PLAN.model_copy(update={"profile": profile})
    info = good_final_video_info(
        plan,
        audio_present=False,
        audio_codec=None,
        audio_sample_rate_hz=None,
        audio_channels=None,
        audio_duration_ms=None,
    )
    report = run_technical_qa(
        plan, profile, info, media_bytes=info.bytes, readback_sha_ok=True, sources_verified=True
    )
    assert report.passed
    assert measured_from_info(info).audio_present is False


def test_measured_from_info() -> None:
    measured = measured_from_info(good_final_video_info(PLAN))
    assert (measured.width, measured.height, measured.audio_codec) == (1080, 1920, "aac")


def test_episode_media_key_and_scene_key_are_distinct() -> None:
    sha = "a" * 64
    assert episode_media_object_key("ep-1", "final_video", sha, "mp4") == (
        f"media/ep-1/final_video/{sha}.mp4"
    )
    assert media_object_key("ep-1", "scene_video", "sb1", sha, "mp4") == (
        f"media/ep-1/scene_video/sb1/{sha}.mp4"
    )
    with pytest.raises(ValueError):
        episode_media_object_key("ep/1", "final_video", sha, "mp4")
    with pytest.raises(ValueError):
        episode_media_object_key("ep-1", "final_video", "A" * 64, "mp4")
