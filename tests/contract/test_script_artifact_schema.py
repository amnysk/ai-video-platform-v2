"""ScriptArtifact スキーマの契約（INV-10）。生成側と取り込み側を同じテストで突き合わせる。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from contracts.artifacts import (
    ARTIFACT_MODELS,
    SCRIPT_ARTIFACT_SCHEMA_VERSION,
    SCRIPT_MAX_SCENES,
    SCRIPT_MAX_TOTAL_DURATION_MS,
    SCRIPT_MIN_SCENES,
    SCRIPT_MIN_TOTAL_DURATION_MS,
    DummyArtifact,
    ScriptArtifact,
    build_dummy_artifact,
    build_script_artifact,
    extract_json_object,
    parse_artifact,
    parse_script_artifact,
)
from contracts.states import ArtifactType
from domain.artifact.hashing import canonical_json_bytes, sha256_hex

_METADATA = {"topic": "応仁の乱", "generator": "codex", "generator_model": "gpt-5"}


def _scenes(count: int = 3, *, duration_ms: int = 6_000) -> list[dict[str, Any]]:
    return [
        {
            "id": f"s{i + 1}",
            "narration": f"ナレーション{i + 1}",
            "visual": f"ビジュアル{i + 1}",
            "duration_ms": duration_ms,
        }
        for i in range(count)
    ]


def _payload(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "episode_id": "ep-1",
        "language": "ja",
        "title": "応仁の乱のはじまり",
        "hook": "京都が11年燃えた理由",
        "scenes": _scenes(),
        "metadata": dict(_METADATA),
    }
    kwargs.update(overrides)
    return build_script_artifact(**kwargs)


def test_generated_script_artifact_matches_the_documented_shape() -> None:
    payload = _payload()
    assert payload == {
        "episode_id": "ep-1",
        "type": "script",
        "schema_version": SCRIPT_ARTIFACT_SCHEMA_VERSION,
        "language": "ja",
        "title": "応仁の乱のはじまり",
        "hook": "京都が11年燃えた理由",
        "scenes": _scenes(),
        "metadata": dict(_METADATA),
    }


def test_script_producer_output_is_accepted_by_the_consumer() -> None:
    parsed = parse_script_artifact(_payload())
    assert isinstance(parsed, ScriptArtifact)
    assert parsed.type is ArtifactType.SCRIPT
    assert parsed.episode_id == "ep-1"
    assert parsed.total_duration_ms == 18_000
    assert parsed.narration == "ナレーション1 ナレーション2 ナレーション3"


def test_script_survives_canonical_json_roundtrip() -> None:
    payload = _payload()
    body = canonical_json_bytes(payload)
    reloaded = json.loads(body)
    assert parse_script_artifact(reloaded) == parse_script_artifact(payload)
    assert sha256_hex(canonical_json_bytes(reloaded)) == sha256_hex(body)


def test_canonical_json_is_stable_under_key_reordering() -> None:
    payload = _payload()
    shuffled = {k: payload[k] for k in reversed(list(payload))}
    shuffled["metadata"] = {k: payload["metadata"][k] for k in reversed(list(payload["metadata"]))}
    assert sha256_hex(canonical_json_bytes(shuffled)) == sha256_hex(canonical_json_bytes(payload))


def test_unknown_script_schema_version_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(ValidationError):
        parse_script_artifact(_payload() | {"schema_version": "99.0"})


def test_script_extra_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_script_artifact(_payload() | {"surprise": 1})

    payload = _payload()
    payload["scenes"][0]["surprise"] = 1
    with pytest.raises(ValidationError):
        parse_script_artifact(payload)


@pytest.mark.parametrize(
    "field",
    ["episode_id", "type", "schema_version", "language", "title", "hook", "scenes", "metadata"],
)
def test_script_missing_required_field_is_rejected(field: str) -> None:
    payload = _payload()
    del payload[field]
    with pytest.raises(ValidationError):
        parse_script_artifact(payload)


@pytest.mark.parametrize("count", [SCRIPT_MIN_SCENES - 1, SCRIPT_MAX_SCENES + 1])
def test_scene_count_bounds_are_enforced_rejects(count: int) -> None:
    with pytest.raises(ValidationError):
        _payload(scenes=_scenes(count, duration_ms=5_000))


@pytest.mark.parametrize("count", [SCRIPT_MIN_SCENES, SCRIPT_MAX_SCENES])
def test_scene_count_bounds_are_enforced_accepts(count: int) -> None:
    parsed = parse_script_artifact(_payload(scenes=_scenes(count, duration_ms=6_000)))
    assert len(parsed.scenes) == count


def test_total_duration_bounds_are_enforced() -> None:
    # 3 x 4000 = 12000 < 15000
    with pytest.raises(ValidationError):
        _payload(scenes=_scenes(3, duration_ms=4_000))
    # 8 x 20000 = 160000 > 60000
    with pytest.raises(ValidationError):
        _payload(scenes=_scenes(8, duration_ms=20_000))
    ok = parse_script_artifact(_payload(scenes=_scenes(3, duration_ms=5_000)))
    assert ok.total_duration_ms == SCRIPT_MIN_TOTAL_DURATION_MS
    ok_max = parse_script_artifact(_payload(scenes=_scenes(3, duration_ms=20_000)))
    assert ok_max.total_duration_ms == SCRIPT_MAX_TOTAL_DURATION_MS


def test_duplicate_scene_id_is_rejected() -> None:
    scenes = _scenes(3)
    scenes[1]["id"] = scenes[0]["id"]
    with pytest.raises(ValidationError):
        _payload(scenes=scenes)


def test_language_has_no_default_and_must_be_declared() -> None:
    assert ScriptArtifact.model_fields["language"].is_required()
    payload = _payload()
    del payload["language"]
    with pytest.raises(ValidationError):
        parse_script_artifact(payload)


def test_unsupported_language_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_script_artifact(_payload() | {"language": "fr"})


def test_narration_is_derived_not_stored() -> None:
    assert "narration" not in ScriptArtifact.model_fields
    assert "total_duration_ms" not in ScriptArtifact.model_fields
    assert "narration" not in _payload()
    assert "total_duration_ms" not in _payload()


def test_every_artifact_type_has_a_registered_model() -> None:
    assert set(ARTIFACT_MODELS) == set(ArtifactType)
    for model in ARTIFACT_MODELS.values():
        assert issubclass(model, BaseModel)


def test_parse_artifact_dispatches_on_type() -> None:
    assert isinstance(parse_artifact(build_dummy_artifact(episode_id="ep-1")), DummyArtifact)
    assert isinstance(parse_artifact(_payload()), ScriptArtifact)


def test_parse_artifact_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        parse_artifact(build_dummy_artifact(episode_id="ep-1") | {"type": "mystery"})
    payload = build_dummy_artifact(episode_id="ep-1")
    del payload["type"]
    with pytest.raises(ValueError):
        parse_artifact(payload)


def test_extract_json_object_strips_code_fences() -> None:
    expected = {"a": 1, "b": {"c": [1, 2]}}
    raw = json.dumps(expected)
    variants = [
        raw,
        f"```json\n{raw}\n```",
        f"以下が結果です。\n```json\n{raw}\n```\nご確認ください。",
    ]
    for variant in variants:
        assert extract_json_object(variant) == expected


def test_extract_json_object_does_not_repair_truncated_json() -> None:
    with pytest.raises(ValueError):
        extract_json_object('```json\n{"a": 1, "b": [1, 2')
    with pytest.raises(ValueError):
        extract_json_object("no json here")
