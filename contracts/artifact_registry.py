"""Artifact の (artifact_type, schema_version) -> model の registry（ADR-0043 §4、INV-54）。

- 未知の版は ``UnsupportedArtifactSchemaError``。黙って別の版として読み替えない。
- 版をまたぐ読み替えは**明示登録した upcaster だけ**（``UPCASTERS``。本番は空）。
- model の実体は ``contracts.artifacts.ARTIFACT_MODELS``（型ごとの唯一の登録）から引く。
  ここは版の宣言だけを持ち、model を再登録しない。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from contracts.artifacts import ARTIFACT_MODELS
from contracts.states import ArtifactType

RegistryKey = tuple[str, str]
Upcaster = Callable[[dict[str, Any]], dict[str, Any]]

#: ADR-0043 が登録する版。既存の storyboard は v1.0 のまま読める。
_REGISTERED: tuple[ArtifactType, ...] = (
    ArtifactType.STORYBOARD,
    ArtifactType.USER_BRIEF,
    ArtifactType.STRATEGY_BRIEF,
    ArtifactType.CREATIVE_BRIEF,
    ArtifactType.QUALITY_REVIEW,
    ArtifactType.LESSON,
    ArtifactType.WORKFLOW_PLAN,
)

ARTIFACT_REGISTRY: dict[RegistryKey, type[BaseModel]] = {
    (t.value, "1.0"): ARTIFACT_MODELS[t] for t in _REGISTERED
}

#: (type, from_version, to_version) -> 関数。本番は空（明示登録が無い限り読み替えない）
UPCASTERS: dict[tuple[str, str, str], Upcaster] = {}


class UnsupportedArtifactSchemaError(ValueError):
    pass


def _version_of(document: Mapping[str, Any]) -> str:
    version = document.get("schema_version")
    if not isinstance(version, str):
        raise UnsupportedArtifactSchemaError("artifact document has no schema_version")
    return version


def parse_artifact(
    artifact_type: ArtifactType | str,
    document: Mapping[str, Any],
    *,
    registry: Mapping[RegistryKey, type[BaseModel]] = ARTIFACT_REGISTRY,
) -> BaseModel:
    type_value = ArtifactType(artifact_type).value
    version = _version_of(document)
    model = registry.get((type_value, version))
    if model is None:
        raise UnsupportedArtifactSchemaError(f"unsupported artifact schema: {type_value}@{version}")
    return model.model_validate(dict(document))


def read_artifact_as(
    artifact_type: ArtifactType | str,
    document: Mapping[str, Any],
    target_version: str,
    *,
    registry: Mapping[RegistryKey, type[BaseModel]] = ARTIFACT_REGISTRY,
    upcasters: Mapping[tuple[str, str, str], Upcaster] = UPCASTERS,
) -> BaseModel:
    """``target_version`` の model として読む。版が違えば登録済みの upcaster が1つあるときだけ。"""
    type_value = ArtifactType(artifact_type).value
    version = _version_of(document)
    if version != target_version:
        upcast = upcasters.get((type_value, version, target_version))
        if upcast is None:
            raise UnsupportedArtifactSchemaError(
                f"no upcaster registered: {type_value}@{version} -> {target_version}"
            )
        document = upcast(dict(document))
    return parse_artifact(type_value, document, registry=registry)
