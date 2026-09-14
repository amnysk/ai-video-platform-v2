"""完成動画の技術検査（ADR-0019 §6）。純粋関数のみ。

``evaluate_technical_qa`` は全項目を評価して結果を返し（不合格も含む）、
``raise_for_failed_checks`` が不合格を失敗クラスの例外へ写像する:

- デコード不能・フレーム0件・保存本体の読み戻し sha256 不一致
  → ``FinalVideoCorruptError``（retryable）
- 入力素材の読み戻し sha256 不一致 → ``RenderInputIntegrityError``（permanent）
- それ以外（解像度・縦横比・fps・尺・codec・音声・バイト数・計画の網羅）
  → ``FinalVideoValidationError``（permanent）

複数が同時に落ちたら上の順で優先する（壊れた出力では他の測定値に意味が無い）。
"""

from __future__ import annotations

from collections.abc import Sequence
from math import gcd

from contracts.artifacts import ProductionManifest
from contracts.render import (
    FINAL_VIDEO_MAX_BYTES,
    FinalVideoMeasured,
    RenderPlan,
    RenderProfile,
    TechnicalQaCheck,
    TechnicalQaReport,
)
from domain.errors import (
    FinalVideoCorruptError,
    FinalVideoValidationError,
    RenderInputIntegrityError,
)
from domain.render.ports import FinalVideoInfo

#: fps の許容差（fps × 1000 の単位）。
FPS_TOLERANCE_MILLIS = 1

# 検査名（``TechnicalQaCheck.check``）。完成動画の JSON に残るので変えない。
CHECK_DECODABLE = "decodable"
CHECK_RESOLUTION = "resolution"
CHECK_ASPECT_RATIO = "aspect_ratio"
CHECK_FPS = "fps"
CHECK_DURATION_MATCHES_PLAN = "duration_matches_plan"
CHECK_DURATION_WITHIN_LIMITS = "duration_within_limits"
CHECK_VIDEO_CODEC = "video_codec"
CHECK_PIX_FMT = "pix_fmt"
CHECK_AUDIO_PRESENT = "audio_present"
CHECK_AUDIO_CODEC = "audio_codec"
CHECK_AUDIO_SAMPLE_RATE = "audio_sample_rate"
CHECK_AUDIO_CHANNELS = "audio_channels"
CHECK_AUDIO_DURATION = "audio_duration"
CHECK_MEDIA_BYTES = "media_bytes"
CHECK_SCENE_COVERAGE = "scene_coverage"
CHECK_VOICE_COVERAGE = "voice_coverage"
CHECK_SUBTITLE_CUES = "subtitle_cues"
CHECK_SOURCE_READBACK = "source_readback"
CHECK_MEDIA_READBACK = "media_readback"

TECHNICAL_QA_CHECK_NAMES: tuple[str, ...] = (
    CHECK_DECODABLE,
    CHECK_RESOLUTION,
    CHECK_ASPECT_RATIO,
    CHECK_FPS,
    CHECK_DURATION_MATCHES_PLAN,
    CHECK_DURATION_WITHIN_LIMITS,
    CHECK_VIDEO_CODEC,
    CHECK_PIX_FMT,
    CHECK_AUDIO_PRESENT,
    CHECK_AUDIO_CODEC,
    CHECK_AUDIO_SAMPLE_RATE,
    CHECK_AUDIO_CHANNELS,
    CHECK_AUDIO_DURATION,
    CHECK_MEDIA_BYTES,
    CHECK_SCENE_COVERAGE,
    CHECK_VOICE_COVERAGE,
    CHECK_SUBTITLE_CUES,
    CHECK_SOURCE_READBACK,
    CHECK_MEDIA_READBACK,
)

_CORRUPT_CHECKS = frozenset({CHECK_DECODABLE, CHECK_MEDIA_READBACK})
_INTEGRITY_CHECKS = frozenset({CHECK_SOURCE_READBACK})


def _check(name: str, passed: bool, detail: str) -> TechnicalQaCheck:
    return TechnicalQaCheck(check=name, passed=passed, detail=detail[:500])


def _ratio(width: int, height: int) -> str:
    g = gcd(width, height) or 1
    return f"{width // g}:{height // g}"


def _plan_checks(plan: RenderPlan, manifest: ProductionManifest | None) -> list[TechnicalQaCheck]:
    total = plan.total_duration_ms
    cursor = 0
    scenes_ok = True
    for scene in plan.scenes:
        if scene.timeline_start_ms != cursor:
            scenes_ok = False
        cursor += scene.timeline_duration_ms
    scenes_ok = scenes_ok and cursor == total
    scene_detail = f"{len(plan.scenes)} scenes, sum {cursor} ms, total {total} ms"
    if manifest is not None:
        expected = [s.scene_id for s in manifest.scenes]
        actual = [s.scene_id for s in plan.scenes]
        if actual != expected:
            scenes_ok = False
            scene_detail = f"plan scenes {actual} != manifest {expected}"

    voices_ok = all(v.start_ms >= 0 and v.end_ms <= total for v in plan.voices)
    voice_ids = [v.script_scene_id for v in plan.voices]
    voices_ok = voices_ok and len(set(voice_ids)) == len(voice_ids)
    voice_detail = f"{len(plan.voices)} voices within [0, {total}] ms"
    if manifest is not None:
        expected_voices = [v.script_scene_id for v in manifest.voices]
        if voice_ids != expected_voices:
            voices_ok = False
            voice_detail = f"plan voices {voice_ids} != manifest {expected_voices}"
    if not voices_ok and manifest is None:
        voice_detail = "voices overlap the bounds or repeat"

    windows = {v.script_scene_id: v for v in plan.voices}
    cues_ok = True
    previous_end = 0
    for cue in plan.subtitle_cues:
        voice = windows.get(cue.script_scene_id)
        if (
            voice is None
            or cue.start_ms < max(voice.start_ms, previous_end)
            or cue.end_ms > min(voice.end_ms, total)
        ):
            cues_ok = False
            break
        previous_end = cue.end_ms
    return [
        _check(CHECK_SCENE_COVERAGE, scenes_ok, scene_detail),
        _check(CHECK_VOICE_COVERAGE, voices_ok, voice_detail),
        _check(
            CHECK_SUBTITLE_CUES,
            cues_ok,
            f"{len(plan.subtitle_cues)} cues ordered inside their voice windows"
            if cues_ok
            else "a subtitle cue is out of order or outside its voice window",
        ),
    ]


def evaluate_technical_qa(
    plan: RenderPlan,
    profile: RenderProfile,
    info: FinalVideoInfo,
    *,
    media_bytes: int,
    readback_sha_ok: bool,
    sources_verified: bool,
    manifest: ProductionManifest | None = None,
) -> tuple[TechnicalQaCheck, ...]:
    """全項目を評価する（例外を投げない）。"""
    limits = profile.limits
    total = plan.total_duration_ms
    checks: list[TechnicalQaCheck] = []
    add = checks.append

    add(
        _check(
            CHECK_DECODABLE,
            info.frames_decoded > 0 and info.decode_errors == 0,
            f"frames_decoded={info.frames_decoded} decode_errors={info.decode_errors}",
        )
    )
    add(
        _check(
            CHECK_RESOLUTION,
            (info.width, info.height) == (profile.width, profile.height)
            and profile == plan.profile,
            f"measured {info.width}x{info.height}, profile {profile.width}x{profile.height}"
            + ("" if profile == plan.profile else " (profile differs from the plan snapshot)"),
        )
    )
    measured_ratio = _ratio(info.width, info.height)
    add(
        _check(
            CHECK_ASPECT_RATIO,
            measured_ratio == profile.aspect_ratio,
            f"measured {measured_ratio}, profile {profile.aspect_ratio}",
        )
    )
    add(
        _check(
            CHECK_FPS,
            abs(info.fps_millis - profile.fps_millis) <= FPS_TOLERANCE_MILLIS,
            f"measured {info.fps_millis} mfps, profile {profile.fps_millis} mfps",
        )
    )
    add(
        _check(
            CHECK_DURATION_MATCHES_PLAN,
            abs(info.duration_ms - total) <= limits.duration_tolerance_ms,
            f"measured {info.duration_ms} ms, plan {total} ms "
            f"(tolerance {limits.duration_tolerance_ms} ms)",
        )
    )
    add(
        _check(
            CHECK_DURATION_WITHIN_LIMITS,
            limits.min_duration_ms <= total <= limits.max_duration_ms
            and limits.min_duration_ms <= info.duration_ms <= limits.max_duration_ms,
            f"plan {total} ms / measured {info.duration_ms} ms within "
            f"[{limits.min_duration_ms}, {limits.max_duration_ms}] ms",
        )
    )
    add(
        _check(
            CHECK_VIDEO_CODEC,
            info.video_codec == profile.video.codec,
            f"measured {info.video_codec}, profile {profile.video.codec}",
        )
    )
    add(
        _check(
            CHECK_PIX_FMT,
            info.pix_fmt == profile.video.pix_fmt,
            f"measured {info.pix_fmt}, profile {profile.video.pix_fmt}",
        )
    )

    audio = profile.audio
    add(
        _check(
            CHECK_AUDIO_PRESENT,
            info.audio_present or not limits.audio_required,
            f"audio_present={info.audio_present} audio_required={limits.audio_required}",
        )
    )
    if info.audio_present:
        add(
            _check(
                CHECK_AUDIO_CODEC,
                info.audio_codec == audio.codec,
                f"measured {info.audio_codec}, profile {audio.codec}",
            )
        )
        add(
            _check(
                CHECK_AUDIO_SAMPLE_RATE,
                info.audio_sample_rate_hz == audio.sample_rate_hz,
                f"measured {info.audio_sample_rate_hz} Hz, profile {audio.sample_rate_hz} Hz",
            )
        )
        add(
            _check(
                CHECK_AUDIO_CHANNELS,
                info.audio_channels == audio.channels,
                f"measured {info.audio_channels} ch, profile {audio.channels} ch",
            )
        )
        add(
            _check(
                CHECK_AUDIO_DURATION,
                info.audio_duration_ms is not None
                and abs(info.audio_duration_ms - total) <= limits.duration_tolerance_ms,
                f"measured audio {info.audio_duration_ms} ms, plan {total} ms",
            )
        )
    else:
        for name in (
            CHECK_AUDIO_CODEC,
            CHECK_AUDIO_SAMPLE_RATE,
            CHECK_AUDIO_CHANNELS,
            CHECK_AUDIO_DURATION,
        ):
            add(_check(name, not limits.audio_required, "no audio stream"))

    add(
        _check(
            CHECK_MEDIA_BYTES,
            0 < media_bytes <= FINAL_VIDEO_MAX_BYTES and info.bytes == media_bytes,
            f"stored {media_bytes} bytes, probed {info.bytes} bytes, cap {FINAL_VIDEO_MAX_BYTES}",
        )
    )
    checks.extend(_plan_checks(plan, manifest))
    add(
        _check(
            CHECK_SOURCE_READBACK,
            sources_verified,
            "input artifacts matched their pinned sha256"
            if sources_verified
            else "an input artifact did not match its pinned sha256",
        )
    )
    add(
        _check(
            CHECK_MEDIA_READBACK,
            readback_sha_ok,
            "stored media sha256 matched" if readback_sha_ok else "stored media sha256 mismatch",
        )
    )
    return tuple(checks)


def raise_for_failed_checks(checks: Sequence[TechnicalQaCheck]) -> None:
    """不合格があれば失敗クラスの例外にする（優先順は module docstring）。"""
    failed = [c for c in checks if not c.passed]
    if not failed:
        return
    summary = "; ".join(f"{c.check}: {c.detail}" for c in failed)[:1000]
    names = {c.check for c in failed}
    if names & _CORRUPT_CHECKS:
        raise FinalVideoCorruptError(f"final video corrupt: {summary}")
    if names & _INTEGRITY_CHECKS:
        raise RenderInputIntegrityError(f"render input integrity: {summary}")
    raise FinalVideoValidationError(f"final video failed technical QA: {summary}")


def run_technical_qa(
    plan: RenderPlan,
    profile: RenderProfile,
    info: FinalVideoInfo,
    *,
    media_bytes: int,
    readback_sha_ok: bool,
    sources_verified: bool,
    manifest: ProductionManifest | None = None,
) -> TechnicalQaReport:
    """評価して、全て合格なら報告を返す。不合格は ``raise_for_failed_checks`` の例外。"""
    checks = evaluate_technical_qa(
        plan,
        profile,
        info,
        media_bytes=media_bytes,
        readback_sha_ok=readback_sha_ok,
        sources_verified=sources_verified,
        manifest=manifest,
    )
    raise_for_failed_checks(checks)
    return TechnicalQaReport(passed=True, checks=checks)


def measured_from_info(info: FinalVideoInfo) -> FinalVideoMeasured:
    """probe の測定値を完成動画の ``measured`` へ写す。"""
    return FinalVideoMeasured(
        width=info.width,
        height=info.height,
        duration_ms=info.duration_ms,
        fps_millis=info.fps_millis,
        video_codec=info.video_codec,
        pix_fmt=info.pix_fmt,
        audio_present=info.audio_present,
        audio_codec=info.audio_codec if info.audio_present else None,
        audio_sample_rate_hz=info.audio_sample_rate_hz if info.audio_present else None,
        audio_channels=info.audio_channels if info.audio_present else None,
    )


__all__ = [
    "FPS_TOLERANCE_MILLIS",
    "TECHNICAL_QA_CHECK_NAMES",
    "evaluate_technical_qa",
    "measured_from_info",
    "raise_for_failed_checks",
    "run_technical_qa",
]
