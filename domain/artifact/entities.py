"""Artifactメタデータのドメイン表現。本体はMinIOにある（INV-9）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from contracts.states import ArtifactType


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    id: str
    episode_id: str
    artifact_type: ArtifactType
    schema_version: str
    bucket: str
    object_key: str
    sha256: str
    created_at: datetime
    #: シーン単位の Artifact（ADR-0018）。Episode 単位なら None。
    scene_id: str | None = None
    #: 同 (episode, type, scene) の中の世代番号（ADR-0012）。
    version: int = 1
