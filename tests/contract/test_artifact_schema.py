"""Artifactスキーマの契約（INV-10）。生成側と取り込み側を同じテストで突き合わせる。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.artifacts import (
    DUMMY_ARTIFACT_SCHEMA_VERSION,
    DummyArtifact,
    build_dummy_artifact,
    parse_artifact,
)
from contracts.states import ArtifactType


def test_generated_dummy_artifact_matches_the_documented_shape() -> None:
    payload = build_dummy_artifact(episode_id="ep-1")
    assert payload == {
        "episode_id": "ep-1",
        "type": "dummy",
        "schema_version": DUMMY_ARTIFACT_SCHEMA_VERSION,
        "message": "workflow completed",
    }


def test_producer_output_is_accepted_by_the_consumer() -> None:
    """生成側 build_dummy_artifact と 取り込み側 parse_artifact の往復。"""
    payload = build_dummy_artifact(episode_id="ep-1")
    parsed = parse_artifact(payload)
    assert isinstance(parsed, DummyArtifact)
    assert parsed.episode_id == "ep-1"
    assert parsed.schema_version == DUMMY_ARTIFACT_SCHEMA_VERSION
    assert parsed.type is ArtifactType.DUMMY


def test_every_artifact_declares_type_and_schema_version() -> None:
    payload = build_dummy_artifact(episode_id="ep-1")
    assert "type" in payload and "schema_version" in payload


def test_unknown_schema_version_is_rejected_rather_than_guessed() -> None:
    """想定外の schema_version を推測して読まない（artifact.md）。"""
    payload = build_dummy_artifact(episode_id="ep-1") | {"schema_version": "99.0"}
    with pytest.raises(ValidationError):
        parse_artifact(payload)


def test_missing_field_is_rejected() -> None:
    payload = build_dummy_artifact(episode_id="ep-1")
    del payload["message"]
    with pytest.raises(ValidationError):
        parse_artifact(payload)


def test_extra_field_is_rejected() -> None:
    payload = build_dummy_artifact(episode_id="ep-1") | {"surprise": 1}
    with pytest.raises(ValidationError):
        parse_artifact(payload)
