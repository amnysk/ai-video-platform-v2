"""Artifactのスキーマ定義（INV-10）。生成側と取り込み側がここを共有する。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from contracts.states import ArtifactType

DUMMY_ARTIFACT_SCHEMA_VERSION = "1.0"
DUMMY_ARTIFACT_MESSAGE = "workflow completed"


class DummyArtifact(BaseModel):
    """Phase 1 の骨組み用ダミー成果物。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: str
    type: Literal[ArtifactType.DUMMY]
    schema_version: Literal["1.0"]
    message: str


def build_dummy_artifact(*, episode_id: str) -> dict[str, Any]:
    """生成側。ここが返す形だけが正。"""
    return {
        "episode_id": episode_id,
        "type": ArtifactType.DUMMY.value,
        "schema_version": DUMMY_ARTIFACT_SCHEMA_VERSION,
        "message": DUMMY_ARTIFACT_MESSAGE,
    }


def parse_artifact(payload: dict[str, Any]) -> DummyArtifact:
    """取り込み側。想定外の schema_version は推測せず ValidationError にする。"""
    return DummyArtifact.model_validate(payload)
