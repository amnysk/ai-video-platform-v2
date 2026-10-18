"""Artifact Registry（ADR-0043 §4、INV-54）。理由: docs/testing/jpmas2-p0a-rationale.md"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from contracts.agentic import UserBrief
from contracts.artifact_registry import (
    ARTIFACT_REGISTRY,
    UPCASTERS,
    UnsupportedArtifactSchemaError,
    parse_artifact,
    read_artifact_as,
)
from contracts.artifacts import StoryboardArtifact, build_storyboard_artifact
from contracts.states import ArtifactType


def _storyboard() -> dict[str, Any]:
    scenes = [
        {
            "scene_id": f"sb{i}",
            "order": i,
            "script_scene_id": f"s{i}",
            "start_ms": (i - 1) * 6000,
            "duration_ms": 6000,
            "visual_kind": "broll",
            "visual_description": f"v{i}",
            "framing": "wide",
            "camera_movement": None,
            "transition_in": None,
        }
        for i in (1, 2, 3)
    ]
    return build_storyboard_artifact(
        episode_id="ep-1",
        source_script={
            "artifact_id": str(uuid.UUID(int=1)),
            "sha256": "a" * 64,
            "schema_version": "1.0",
        },
        scenes=scenes,
        total_duration_ms=18_000,
        metadata={"generator": "g", "generator_model": "m", "generation_spec_id": "s@abc/sha256:0"},
    )


def _brief() -> dict[str, Any]:
    return UserBrief(theme="x", template_id="t", template_version="1").model_dump(mode="json")


def test_the_registry_holds_the_adr_0043_entries_and_the_adr_0047_fact_check_report() -> None:
    assert set(ARTIFACT_REGISTRY) == {
        ("storyboard", "1.0"),
        ("user_brief", "1.0"),
        ("strategy_brief", "1.0"),
        ("creative_brief", "1.0"),
        ("quality_review", "1.0"),
        ("lesson", "1.0"),
        ("workflow_plan", "1.0"),
        ("fact_check_report", "1.0"),  # ADR-0047（Historian の成果物）
    }
    assert UPCASTERS == {}


def test_a_stored_storyboard_v1_is_readable_through_the_registry() -> None:
    parsed = parse_artifact(ArtifactType.STORYBOARD, _storyboard())
    assert isinstance(parsed, StoryboardArtifact)
    assert parse_artifact("storyboard", _storyboard()) == parsed


def test_a_new_artifact_round_trips() -> None:
    assert isinstance(parse_artifact(ArtifactType.USER_BRIEF, _brief()), UserBrief)


@pytest.mark.parametrize("version", ["2.0", "0.9", "1", "1.0.0", ""])
def test_an_unknown_schema_version_is_rejected_not_reinterpreted(version: str) -> None:
    with pytest.raises(UnsupportedArtifactSchemaError):
        parse_artifact(ArtifactType.USER_BRIEF, {**_brief(), "schema_version": version})


def test_a_missing_or_non_string_version_is_rejected() -> None:
    doc = _brief()
    del doc["schema_version"]
    with pytest.raises(UnsupportedArtifactSchemaError):
        parse_artifact(ArtifactType.USER_BRIEF, doc)
    with pytest.raises(UnsupportedArtifactSchemaError):
        parse_artifact(ArtifactType.USER_BRIEF, {**_brief(), "schema_version": 1.0})


def test_an_unregistered_type_is_rejected() -> None:
    with pytest.raises(UnsupportedArtifactSchemaError):
        parse_artifact(ArtifactType.SCRIPT, {"schema_version": "1.0"})


def test_reading_across_versions_needs_an_explicitly_registered_upcaster() -> None:
    class BriefV2(BaseModel):
        theme_text: str
        schema_version: str = "2.0"

    registry: dict[tuple[str, str], type[BaseModel]] = {
        ("user_brief", "1.0"): UserBrief,
        ("user_brief", "2.0"): BriefV2,
    }
    v1 = _brief()

    # 登録が無ければ読み替えない（同じ registry に v2 の model があっても）
    with pytest.raises(UnsupportedArtifactSchemaError, match="no upcaster"):
        read_artifact_as("user_brief", v1, "2.0", registry=registry, upcasters={})

    def upcast(doc: dict[str, Any]) -> dict[str, Any]:
        return {"theme_text": doc["theme"], "schema_version": "2.0"}

    got = read_artifact_as(
        "user_brief", v1, "2.0", registry=registry, upcasters={("user_brief", "1.0", "2.0"): upcast}
    )
    assert isinstance(got, BriefV2) and got.theme_text == "x"
    # 同じ版の読みは upcaster を使わない
    assert isinstance(read_artifact_as("user_brief", v1, "1.0", registry=registry), UserBrief)
    # 逆向き（v2 -> v1）は登録が無いので拒否
    with pytest.raises(UnsupportedArtifactSchemaError):
        read_artifact_as(
            "user_brief",
            {"theme_text": "x", "schema_version": "2.0"},
            "1.0",
            registry=registry,
            upcasters={("user_brief", "1.0", "2.0"): upcast},
        )
