"""Artifactのオブジェクトキー規約（docs/domain/artifact.md）。

キーの形をここ以外で組み立てないこと。散文やコードにパスを散らさない。
"""

from __future__ import annotations

ARTIFACT_KEY_PREFIX = "artifacts"


def artifact_object_key(episode_id: str, artifact_type: str, sha256: str) -> str:
    """content-addressed なキー。同じ内容なら必ず同じキーになる。"""
    return f"{ARTIFACT_KEY_PREFIX}/{episode_id}/{artifact_type}/{sha256}.json"
