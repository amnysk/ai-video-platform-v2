"""research の成果物のオブジェクトキー（ADR-0037 §5）。純粋関数のみ。

Episode の成果物（``artifacts/`` 配下。``domain/artifact/keys.py``）とは別の接頭辞に置き、
同じ ArtifactStore を使っても本番の成果物と混ざらない。キーの形をここ以外で組み立てない。
"""

from __future__ import annotations

import re
import uuid

from contracts.research import ResearchArtifactType, ResearchCall

RESEARCH_KEY_PREFIX = "research"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _check_uuid(value: str, name: str) -> None:
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be a canonical UUID: {value!r}") from exc
    if canonical != value:
        raise ValueError(f"{name} must be a canonical UUID: {value!r}")


def research_artifact_object_key(
    request_id: str, artifact_type: ResearchArtifactType, sha256: str
) -> str:
    """content-addressed なキー: ``research/{request_id}/{artifact_type}/{sha256}.json``。"""
    _check_uuid(request_id, "request_id")
    if not _SHA_RE.match(sha256):
        raise ValueError(f"sha256 must be 64 lowercase hex: {sha256!r}")
    type_segment = ResearchArtifactType(artifact_type).value
    return f"{RESEARCH_KEY_PREFIX}/{request_id}/{type_segment}/{sha256}.json"


def research_raw_object_key(request_id: str, call: ResearchCall, call_id: str) -> str:
    """外部呼び出し 1 件の生データ: ``research/{request_id}/raw/{call}/{call_id}.json``。

    成果物ではない（``research_artifacts`` に記録しない）。台帳の行（``call_id``）ごとに 1 つで、
    「呼んで結果を受け取った」証拠になる。再実行はこれを読み、同じ呼び出しを送り直さない。
    ``raw`` は ``ResearchArtifactType`` の値と重ならない。
    """
    _check_uuid(request_id, "request_id")
    _check_uuid(call_id, "call_id")
    return f"{RESEARCH_KEY_PREFIX}/{request_id}/raw/{ResearchCall(call).value}/{call_id}.json"


__all__ = ["RESEARCH_KEY_PREFIX", "research_artifact_object_key", "research_raw_object_key"]
