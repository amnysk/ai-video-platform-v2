"""Artifact再利用の完全性判定（ADR-0033）。純粋関数のみ。DB・MinIO・HTTPに触れない（INV-6）。

呼び出し側（``infrastructure/artifact/verify.py``）が実体から集めた事実
（``ArtifactVerificationFacts``）を渡し、ここは verdict を1つに決めるだけ。

優先順位（安全側に倒す。推測しない）:
``MISSING`` > ``CORRUPT_SCHEMA`` > ``CORRUPT_HASH`` > ``VERSION_MISMATCH`` > ``REUSABLE``
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ArtifactVerdict(StrEnum):
    """再利用してよいかの判定結果。``REUSABLE`` 以外は再利用しない（呼び出し側は None 扱い）。"""

    REUSABLE = "reusable"
    MISSING = "missing"
    CORRUPT_HASH = "corrupt_hash"
    CORRUPT_SCHEMA = "corrupt_schema"
    VERSION_MISMATCH = "version_mismatch"


@dataclass(frozen=True, slots=True)
class ArtifactVerificationFacts:
    """実体検証で集めた事実。I/O層がこの形に詰めて渡す。

    ``media_*`` / ``profile_id_valid`` は、対応する ``*_required`` /
    ``profile_check_applicable`` が False のときは無視される（既定値のままでよい）。
    """

    #: JSON記述子オブジェクトが MinIO に存在するか。
    descriptor_exists: bool
    #: JSON記述子が現行の contracts で parse できたか（schema_version 含む）。
    #: ``descriptor_exists=False`` のときは意味を持たない（未検査のまま渡してよい）。
    schema_valid: bool
    #: この artifact_type が入れ子の ``MediaDescriptor`` を持つか（SCENE_*/FINAL_VIDEO）。
    media_required: bool
    #: 入れ子のメディア実体が MinIO に存在するか。``media_required=False`` なら無視。
    media_exists: bool = True
    #: JSON記述子自身の size と sha256 が記録値と一致したか。
    descriptor_content_verified: bool = True
    #: 入れ子メディアの size と sha256 が記録値と一致したか。``media_required=False`` なら無視。
    media_content_verified: bool = True
    #: この artifact_type に「現在有効な生成設定版」という概念があるか。
    profile_check_applicable: bool = False
    #: 記録された生成設定版が現在有効な値と一致したか。``profile_check_applicable=False`` なら無視。
    profile_id_valid: bool = True


def decide_verdict(facts: ArtifactVerificationFacts) -> ArtifactVerdict:
    """事実から verdict を1つ決める。判定できない事実は安全側（非 REUSABLE）に倒す。"""
    if not facts.descriptor_exists:
        return ArtifactVerdict.MISSING
    if not facts.schema_valid:
        return ArtifactVerdict.CORRUPT_SCHEMA
    if facts.media_required and not facts.media_exists:
        return ArtifactVerdict.MISSING
    if not facts.descriptor_content_verified:
        return ArtifactVerdict.CORRUPT_HASH
    if facts.media_required and not facts.media_content_verified:
        return ArtifactVerdict.CORRUPT_HASH
    if facts.profile_check_applicable and not facts.profile_id_valid:
        return ArtifactVerdict.VERSION_MISMATCH
    return ArtifactVerdict.REUSABLE


__all__ = ["ArtifactVerdict", "ArtifactVerificationFacts", "decide_verdict"]
