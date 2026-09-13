"""Production の Artifact 契約（INV-10 / ADR-0017）。生成側と取り込み側を同じテストで照合する。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from contracts.artifacts import (
    ARTIFACT_MODELS,
    ProductionManifest,
    SceneImageArtifact,
    SceneVideoArtifact,
    SceneVoiceArtifact,
    build_production_manifest,
    build_scene_image_artifact,
    build_scene_video_artifact,
    build_scene_voice_artifact,
    parse_artifact,
    parse_production_manifest,
    parse_scene_image_artifact,
    parse_scene_video_artifact,
    parse_scene_voice_artifact,
)
from contracts.states import ArtifactType
from tests.support.production import GENERATOR, digest_ref, media_descriptor, source_ref


def image_kwargs(**overrides: Any) -> dict[str, Any]:
    return {
        "episode_id": "ep-1",
        "source_storyboard": source_ref(),
        "scene_id": "sb1",
        "media": media_descriptor("image/png"),
        "width": 1080,
        "height": 1920,
        "generator": GENERATOR,
        **overrides,
    }


def voice_kwargs(**overrides: Any) -> dict[str, Any]:
    return {
        "episode_id": "ep-1",
        "source_storyboard": source_ref(),
        "source_script": source_ref("b" * 64),
        "script_scene_id": "s2",
        "storyboard_scene_ids": ["sb2", "sb3"],
        "language": "ja",
        "voice_id": "voice-1",
        "media": media_descriptor("audio/wav"),
        "duration_ms": 4200,
        "sample_rate_hz": 22050,
        "channels": 1,
        "generator": GENERATOR,
        **overrides,
    }


def video_kwargs(**overrides: Any) -> dict[str, Any]:
    return {
        "episode_id": "ep-1",
        "source_storyboard": source_ref(),
        "scene_id": "sb1",
        "source_image": digest_ref(),
        "media": media_descriptor("video/mp4"),
        "duration_ms": 5000,
        "requested_duration_ms": 5000,
        "width": 720,
        "height": 1280,
        "fps_millis": 24000,
        "generator": GENERATOR,
        **overrides,
    }


def manifest_kwargs(**overrides: Any) -> dict[str, Any]:
    return {
        "episode_id": "ep-1",
        "source_storyboard": source_ref(),
        "source_script": source_ref("b" * 64),
        "scenes": [{"scene_id": "sb1", "image": digest_ref(), "video": digest_ref("c" * 64)}],
        "voices": [{"script_scene_id": "s1", **digest_ref("d" * 64)}],
        **overrides,
    }


CASES = [
    (build_scene_image_artifact, parse_scene_image_artifact, image_kwargs, SceneImageArtifact),
    (build_scene_voice_artifact, parse_scene_voice_artifact, voice_kwargs, SceneVoiceArtifact),
    (build_scene_video_artifact, parse_scene_video_artifact, video_kwargs, SceneVideoArtifact),
    (build_production_manifest, parse_production_manifest, manifest_kwargs, ProductionManifest),
]


@pytest.mark.parametrize(("build", "parse", "kwargs", "model"), CASES)
def test_build_then_parse_roundtrip(build, parse, kwargs, model) -> None:
    payload = build(**kwargs())
    assert payload["schema_version"] == "1.0"
    parsed = parse(payload)
    assert isinstance(parsed, model)
    assert isinstance(parse_artifact(payload), model)
    assert parsed.model_dump(mode="json") == payload


@pytest.mark.parametrize(("build", "parse", "kwargs", "model"), CASES)
def test_unknown_fields_and_versions_are_rejected(build, parse, kwargs, model) -> None:
    payload = build(**kwargs())
    with pytest.raises(ValidationError):
        parse({**payload, "provider_response": {"raw": 1}})
    with pytest.raises(ValidationError):
        parse({**payload, "schema_version": "2.0"})


@pytest.mark.parametrize(("build", "parse", "kwargs", "model"), CASES)
def test_models_are_frozen(build, parse, kwargs, model) -> None:
    parsed = parse(build(**kwargs()))
    with pytest.raises(ValidationError):
        parsed.episode_id = "other"


def _field_names(model: type[BaseModel], seen: set[type] | None = None) -> set[str]:
    seen = seen or set()
    names: set[str] = set()
    for name, info in model.model_fields.items():
        names.add(name)
        for arg in getattr(info.annotation, "__args__", ()) + (info.annotation,):
            if isinstance(arg, type) and issubclass(arg, BaseModel) and arg not in seen:
                seen.add(arg)
                names |= _field_names(arg, seen)
    return names


@pytest.mark.parametrize("model", [c[3] for c in CASES])
def test_contracts_do_not_carry_provider_job_ids_or_raw_responses(model) -> None:
    names = _field_names(model)
    assert not any("job" in n or "raw" in n or "seed" in n or "narration" in n for n in names)


def test_every_production_type_is_registered() -> None:
    for artifact_type, model in (
        (ArtifactType.SCENE_IMAGE, SceneImageArtifact),
        (ArtifactType.SCENE_VOICE, SceneVoiceArtifact),
        (ArtifactType.SCENE_VIDEO, SceneVideoArtifact),
        (ArtifactType.PRODUCTION_MANIFEST, ProductionManifest),
    ):
        assert ARTIFACT_MODELS[artifact_type] is model


@pytest.mark.parametrize(
    ("build", "overrides"),
    [
        (build_scene_image_artifact, {"media": media_descriptor("image/gif")}),
        (build_scene_image_artifact, {"media": media_descriptor("video/mp4")}),
        (build_scene_image_artifact, {"scene_id": "s1"}),
        (build_scene_image_artifact, {"media": {**media_descriptor(), "bytes": 0}}),
        (build_scene_image_artifact, {"media": {**media_descriptor(), "sha256": "XYZ"}}),
        (build_scene_image_artifact, {"source_storyboard": {**source_ref(), "artifact_id": "x"}}),
        (build_scene_voice_artifact, {"media": media_descriptor("audio/mpeg")}),
        (build_scene_voice_artifact, {"storyboard_scene_ids": []}),
        (build_scene_voice_artifact, {"storyboard_scene_ids": ["sb1", "sb1"]}),
        (build_scene_voice_artifact, {"script_scene_id": "sb1"}),
        (build_scene_voice_artifact, {"channels": 3}),
        (build_scene_voice_artifact, {"language": "fr"}),
        (build_scene_video_artifact, {"media": media_descriptor("image/png")}),
        (build_scene_video_artifact, {"fps_millis": 23.976}),
        (build_scene_video_artifact, {"source_image": {"artifact_id": "x", "sha256": "a" * 64}}),
    ],
)
def test_invalid_payloads_are_rejected(build, overrides) -> None:
    kwargs = {
        build_scene_image_artifact: image_kwargs,
        build_scene_voice_artifact: voice_kwargs,
        build_scene_video_artifact: video_kwargs,
    }[build]
    with pytest.raises(ValidationError):
        build(**kwargs(**overrides))


def test_video_has_audio_is_always_false() -> None:
    payload = build_scene_video_artifact(**video_kwargs())
    assert payload["has_audio"] is False
    with pytest.raises(ValidationError):
        parse_scene_video_artifact({**payload, "has_audio": True})


@pytest.mark.parametrize(
    "overrides",
    [
        {"scenes": []},
        {"voices": []},
        {
            "scenes": [
                {"scene_id": "sb1", "image": digest_ref(), "video": digest_ref()},
                {"scene_id": "sb1", "image": digest_ref(), "video": digest_ref()},
            ]
        },
        {
            "voices": [
                {"script_scene_id": "s1", **digest_ref()},
                {"script_scene_id": "s1", **digest_ref()},
            ]
        },
    ],
)
def test_manifest_rejects_empty_or_duplicate_entries(overrides) -> None:
    with pytest.raises(ValidationError):
        build_production_manifest(**manifest_kwargs(**overrides))
