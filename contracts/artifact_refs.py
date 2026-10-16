"""Artifact 契約が共有する参照型と語彙のパターン（INV-10）。

``contracts/artifacts.py`` と ``contracts/render.py`` の両方が使う。render の契約（ADR-0019）が
artifacts を import し、artifacts が完成動画のモデルを登録するため、共有部品をここへ分けて
循環 import を避ける。定義はここが唯一で、``contracts.artifacts`` からも同じ名前で import できる。
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 台本シーンIDの語彙。``s1`` .. ``s99``。
SCRIPT_SCENE_ID_PATTERN = r"^s[0-9]{1,2}$"
#: storyboard シーンIDの語彙。``sb1`` .. ``sb99``。システムが order から採番する。
STORYBOARD_SCENE_ID_PATTERN = r"^sb[0-9]{1,2}$"
SHA256_HEX_PATTERN = r"^[0-9a-f]{64}$"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def check_canonical_uuid(value: str) -> str:
    if str(uuid.UUID(value)) != value:
        raise ValueError(f"artifact_id must be a canonical UUID: {value!r}")
    return value


class SourceArtifactRef(FrozenModel):
    """入力 Artifact の固定（artifact_id / sha256 / schema_version）。"""

    artifact_id: str
    sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    schema_version: Literal["1.0"]

    @field_validator("artifact_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        return check_canonical_uuid(value)


#: 入力 storyboard の固定。
SourceStoryboardRef = SourceArtifactRef
#: 入力台本の固定（``StoryboardSourceScript`` と同形）。
SourceScriptRef = SourceArtifactRef


class ArtifactDigestRef(FrozenModel):
    """別 Artifact への参照（artifact_id / sha256）。"""

    artifact_id: str
    sha256: str = Field(pattern=SHA256_HEX_PATTERN)

    @field_validator("artifact_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        return check_canonical_uuid(value)
