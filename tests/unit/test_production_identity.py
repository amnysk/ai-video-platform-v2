"""シーン素材の input_hash（ADR-0012 / ADR-0017）。"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from domain.production import identity
from domain.production.identity import (
    idempotency_key,
    image_input_hash,
    narration_sha256,
    video_input_hash,
    voice_input_hash,
)
from domain.script.identity import idempotency_key as script_idempotency_key

IMAGE: dict[str, Any] = {
    "episode_id": "ep-1",
    "artifact_type": "scene_image",
    "schema_version": "1.0",
    "storyboard_sha256": "a" * 64,
    "scene_id": "sb1",
    "visual_description": "土器のクローズアップ",
    "visual_kind": "broll",
    "framing": "close-up",
    "style_profile_id": "style-1",
    "generator_id": "gen-image",
    "generation_profile_id": "profile-1",
}
VOICE: dict[str, Any] = {
    "episode_id": "ep-1",
    "artifact_type": "scene_voice",
    "schema_version": "1.0",
    "script_sha256": "b" * 64,
    "script_scene_id": "s1",
    "storyboard_sha256": "a" * 64,
    "storyboard_scene_ids": ("sb1", "sb2"),
    "narration_sha256": narration_sha256("縄文土器"),
    "voice_id": "voice-1",
    "language": "ja",
    "speed_permille": 1000,
    "generator_id": "gen-voice",
    "generation_profile_id": "profile-v",
}
VIDEO: dict[str, Any] = {
    "episode_id": "ep-1",
    "artifact_type": "scene_video",
    "schema_version": "1.0",
    "storyboard_sha256": "a" * 64,
    "scene_id": "sb1",
    "source_image_sha256": "c" * 64,
    "visual_description": "土器のクローズアップ",
    "camera_movement": "slow push in",
    "transition_in": None,
    "requested_duration_ms": 5000,
    "generator_id": "gen-video",
    "generation_profile_id": "profile-2",
}

CASES = [(image_input_hash, IMAGE), (voice_input_hash, VOICE), (video_input_hash, VIDEO)]


def _changed(value: Any) -> Any:
    if value is None:
        return "set"
    if isinstance(value, tuple):
        return (*value, "sb9")
    if isinstance(value, int):
        return value + 1
    return value + "x"


@pytest.mark.parametrize(("fn", "base"), CASES)
def test_hash_is_stable_and_hex(fn, base) -> None:
    first = fn(**base)
    assert first == fn(**dict(reversed(list(base.items()))))
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)


@pytest.mark.parametrize(("fn", "base"), CASES)
def test_hash_is_sensitive_to_every_field(fn, base) -> None:
    original = fn(**base)
    for key, value in base.items():
        assert fn(**{**base, key: _changed(value)}) != original, key


@pytest.mark.parametrize(("fn", "base"), CASES)
def test_hash_excludes_attempt_job_time_and_run(fn, base) -> None:
    params = set(inspect.signature(fn).parameters)
    for forbidden in ("round", "attempt", "job_id", "run_id", "workflow_id", "created_at", "seed"):
        assert forbidden not in params
    assert set(base) == params


def test_voice_hash_ignores_storyboard_scene_order() -> None:
    """storyboard の再計画で古い音声を再利用しない。ただし参照集合の並びは意味を持たない。"""
    swapped = {**VOICE, "storyboard_scene_ids": ("sb2", "sb1")}
    assert voice_input_hash(**swapped) == voice_input_hash(**VOICE)
    assert voice_input_hash(**{**VOICE, "storyboard_sha256": "c" * 64}) != voice_input_hash(
        **VOICE
    )


def test_image_hash_ignores_motion_fields() -> None:
    params = set(inspect.signature(image_input_hash).parameters)
    assert "camera_movement" not in params and "transition_in" not in params


def test_hashes_of_different_media_do_not_collide_on_shared_fields() -> None:
    assert image_input_hash(**IMAGE) != video_input_hash(**VIDEO)


def test_idempotency_key_is_reused_from_script_identity() -> None:
    assert identity.idempotency_key is script_idempotency_key
    assert idempotency_key(provider="fal_image", input_hash="h", round=1) != idempotency_key(
        provider="fal_image", input_hash="h", round=2
    )


def test_narration_hash_depends_on_text_only() -> None:
    assert narration_sha256("a") == narration_sha256("a")
    assert narration_sha256("a") != narration_sha256("b")
