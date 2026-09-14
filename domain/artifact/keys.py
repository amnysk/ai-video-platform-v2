"""Artifactのオブジェクトキー規約（docs/domain/artifact.md）。

キーの形をここ以外で組み立てないこと。散文やコードにパスを散らさない。
"""

from __future__ import annotations

import re

ARTIFACT_KEY_PREFIX = "artifacts"
#: メディア本体（画像・音声・動画のバイナリ）の置き場（ADR-0017）。
MEDIA_KEY_PREFIX = "media"

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_EXT_RE = re.compile(r"^[a-z0-9]{1,8}$")


def _segment(value: str, field: str) -> str:
    if not _SEGMENT_RE.match(value):
        raise ValueError(f"invalid {field} for an object key: {value!r}")
    return value


def artifact_object_key(
    episode_id: str, artifact_type: str, sha256: str, scene_id: str | None = None
) -> str:
    """content-addressed なキー。同じ内容なら必ず同じキーになる。

    シーン単位の Artifact（ADR-0018）は ``scene_id`` を型の下に挟む。
    ``scene_id`` を省略した場合は Phase 3 までと完全に同じキーになる。
    """
    if scene_id is None:
        return f"{ARTIFACT_KEY_PREFIX}/{episode_id}/{artifact_type}/{sha256}.json"
    scene = _segment(scene_id, "scene_id")
    return f"{ARTIFACT_KEY_PREFIX}/{episode_id}/{artifact_type}/{scene}/{sha256}.json"


def media_object_key(
    episode_id: str, artifact_type: str, scene_id: str, sha256: str, extension: str
) -> str:
    """メディア本体のキー: ``media/{episode}/{type}/{scene}/{sha256}.{ext}``。"""
    if not _SHA_RE.match(sha256):
        raise ValueError(f"sha256 must be 64 lowercase hex: {sha256!r}")
    if not _EXT_RE.match(extension):
        raise ValueError(f"invalid extension: {extension!r}")
    return (
        f"{MEDIA_KEY_PREFIX}/{_segment(episode_id, 'episode_id')}/"
        f"{_segment(artifact_type, 'artifact_type')}/{_segment(scene_id, 'scene_id')}/"
        f"{sha256}.{extension}"
    )


def episode_media_object_key(
    episode_id: str, artifact_type: str, sha256: str, extension: str
) -> str:
    """Episode 単位のメディア本体のキー: ``media/{episode}/{type}/{sha256}.{ext}``（ADR-0019）。

    完成動画のようにシーンを持たない Artifact 用。シーン単位のキーとは階層の深さで区別される。
    """
    if not _SHA_RE.match(sha256):
        raise ValueError(f"sha256 must be 64 lowercase hex: {sha256!r}")
    if not _EXT_RE.match(extension):
        raise ValueError(f"invalid extension: {extension!r}")
    return (
        f"{MEDIA_KEY_PREFIX}/{_segment(episode_id, 'episode_id')}/"
        f"{_segment(artifact_type, 'artifact_type')}/{sha256}.{extension}"
    )
