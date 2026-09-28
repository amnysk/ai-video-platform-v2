"""ADR-0035 (4): シーン単位の input_hash（方式2）とレシピ版を除いた content fingerprint。"""

from __future__ import annotations

from typing import Any

import pytest

from domain.production.identity import (
    IDENTITY_SCHEME_VERSION,
    image_content_fingerprint,
    image_input_hash,
    image_input_hash_v2,
    recipe_family,
    recipe_version_candidates,
    video_content_fingerprint,
    video_input_hash_v2,
)

IMAGE_V2: dict[str, Any] = {
    "episode_id": "ep-1",
    "artifact_type": "scene_image",
    "schema_version": "1.0",
    "storyboard_sha256": "a" * 64,
    "scene_id": "sb1",
    "scene_fingerprint": "f" * 64,
    "style_profile_id": "vertical-short-cinematic-v1:prompt-v2",
    "generator_id": "gen-image",
    "generation_profile_id": "fake-image-profile-v1",
}
VIDEO_V2: dict[str, Any] = {
    "episode_id": "ep-1",
    "artifact_type": "scene_video",
    "schema_version": "1.0",
    "storyboard_sha256": "a" * 64,
    "scene_id": "sb1",
    "scene_fingerprint": "f" * 64,
    "source_image_sha256": "c" * 64,
    "requested_duration_ms": 4000,
    "generator_id": "gen-video",
    "generator_profile_id": "fake-video-profile-v1",
    "motion_profile_id": "vertical-short-subtle-motion-v1:video-prompt-v2",
}


def test_scheme_version_is_two() -> None:
    assert IDENTITY_SCHEME_VERSION == 2


@pytest.mark.parametrize(
    ("profile_id", "family"),
    [
        ("vertical-short-cinematic-v1:prompt-v2", "vertical-short-cinematic-v1:prompt-v*"),
        (
            "vertical-short-subtle-motion-v1:video-prompt-v13",
            "vertical-short-subtle-motion-v1:video-prompt-v*",
        ),
        (
            "fake-video-profile-v1+vertical-short-subtle-motion-v1:video-prompt-v2",
            "fake-video-profile-v1+vertical-short-subtle-motion-v1:video-prompt-v*",
        ),
        # 生成器自身の版（モデル・パラメータ）はレシピではない。変われば作り直す
        ("fake-image-profile-v1", "fake-image-profile-v1"),
    ],
)
def test_recipe_family_strips_only_the_prompt_builder_version(profile_id, family) -> None:
    assert recipe_family(profile_id) == family


def test_recipe_version_candidates_enumerate_every_earlier_version() -> None:
    assert recipe_version_candidates("s:prompt-v3") == ["s:prompt-v1", "s:prompt-v2", "s:prompt-v3"]
    assert recipe_version_candidates("fake-image-profile-v1") == ["fake-image-profile-v1"]


def test_v2_image_hash_is_in_a_different_namespace_from_the_legacy_hash() -> None:
    """同じ材料でも方式1と方式2の hash は衝突しない（旧行を新方式の一致と誤認しない）。"""
    legacy = image_input_hash(
        episode_id="ep-1",
        artifact_type="scene_image",
        schema_version="1.0",
        storyboard_sha256="a" * 64,
        scene_id="sb1",
        visual_description="x",
        visual_kind="broll",
        framing=None,
        style_profile_id=IMAGE_V2["style_profile_id"],
        generator_id="gen-image",
        generation_profile_id="fake-image-profile-v1",
    )
    assert image_input_hash_v2(**IMAGE_V2) != legacy


def test_image_input_hash_changes_with_the_recipe_version_but_content_fingerprint_does_not() -> (
    None
):
    bumped = {**IMAGE_V2, "style_profile_id": "vertical-short-cinematic-v1:prompt-v3"}
    assert image_input_hash_v2(**IMAGE_V2) != image_input_hash_v2(**bumped)
    assert image_content_fingerprint(**IMAGE_V2) == image_content_fingerprint(**bumped)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scene_fingerprint", "e" * 64),
        ("generator_id", "other"),
        ("generation_profile_id", "fake-image-profile-v2"),
        ("storyboard_sha256", "b" * 64),
        ("scene_id", "sb2"),
    ],
)
def test_image_content_fingerprint_changes_when_the_content_changes(field, value) -> None:
    assert image_content_fingerprint(**IMAGE_V2) != image_content_fingerprint(
        **{**IMAGE_V2, field: value}
    )


def test_video_content_fingerprint_ignores_only_the_motion_prompt_version() -> None:
    bumped = {**VIDEO_V2, "motion_profile_id": "vertical-short-subtle-motion-v1:video-prompt-v3"}
    assert video_input_hash_v2(**VIDEO_V2) != video_input_hash_v2(**bumped)
    assert video_content_fingerprint(**VIDEO_V2) == video_content_fingerprint(**bumped)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_image_sha256", "d" * 64),  # 画像が変われば動画も作り直す
        ("scene_fingerprint", "e" * 64),
        ("requested_duration_ms", 5000),
        ("generator_id", "other"),
        ("generator_profile_id", "fake-video-profile-v2"),
    ],
)
def test_video_content_fingerprint_changes_when_the_content_changes(field, value) -> None:
    assert video_content_fingerprint(**VIDEO_V2) != video_content_fingerprint(
        **{**VIDEO_V2, field: value}
    )
