"""research の成果物のオブジェクトキー（ADR-0037 §5）。純粋関数のみ。

Episode の成果物（``artifacts/`` 配下。``domain/artifact/keys.py``）とは別の接頭辞に置き、
同じ ArtifactStore を使っても本番の成果物と混ざらない。キーの形をここ以外で組み立てない。
"""

from __future__ import annotations

import re
import uuid

from contracts.research import ResearchArtifactType

RESEARCH_KEY_PREFIX = "research"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def research_artifact_object_key(
    request_id: str, artifact_type: ResearchArtifactType, sha256: str
) -> str:
    """content-addressed なキー: ``research/{request_id}/{artifact_type}/{sha256}.json``。"""
    if str(uuid.UUID(request_id)) != request_id:
        raise ValueError(f"request_id must be a canonical UUID: {request_id!r}")
    if not _SHA_RE.match(sha256):
        raise ValueError(f"sha256 must be 64 lowercase hex: {sha256!r}")
    type_segment = ResearchArtifactType(artifact_type).value
    return f"{RESEARCH_KEY_PREFIX}/{request_id}/{type_segment}/{sha256}.json"


__all__ = ["RESEARCH_KEY_PREFIX", "research_artifact_object_key"]
