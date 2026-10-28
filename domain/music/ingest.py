"""カタログ取込の検査（INV-99、T12 / T27）。純粋関数。取込自体は管理者の明示操作で、ここは判定だけ。

- 実ファイルの sha256 を呼び出し側が計算して渡す（ここでファイルを読まない）
- 同じ sha256 は新規登録せず既存を再利用する。既存のライセンス事実は再取込の申告で変わらない
- 原本は content-addressed で immutable。同じ ``track_id`` で中身が違うものは変造として拒否する
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from contracts.artifact_refs import SHA256_HEX_PATTERN
from contracts.music import BgmTrack

ORIGINALS_PREFIX = "music/originals/"


class IngestOutcome(StrEnum):
    ACCEPT = "accept"
    DUPLICATE = "duplicate"
    REJECT = "reject"


class IngestRejectReason(StrEnum):
    ACTUAL_SHA_MALFORMED = "actual_sha_malformed"
    SHA_MISMATCH = "sha_mismatch"
    OBJECT_KEY_NOT_CONTENT_ADDRESSED = "object_key_not_content_addressed"
    TRACK_ID_CONFLICT = "track_id_conflict"


@dataclass(frozen=True, slots=True)
class IngestDecision:
    outcome: IngestOutcome
    reason: IngestRejectReason | None = None
    existing_track_id: str | None = None


def _reject(reason: IngestRejectReason) -> IngestDecision:
    return IngestDecision(IngestOutcome.REJECT, reason=reason)


def ingest_check(
    existing_catalog: Iterable[BgmTrack], new_track: BgmTrack, actual_sha256: str
) -> IngestDecision:
    if not re.fullmatch(SHA256_HEX_PATTERN, actual_sha256):
        return _reject(IngestRejectReason.ACTUAL_SHA_MALFORMED)
    if new_track.file_sha256 != actual_sha256:
        return _reject(IngestRejectReason.SHA_MISMATCH)
    key_pattern = rf"{re.escape(ORIGINALS_PREFIX)}{actual_sha256}(\.[A-Za-z0-9]+)?"
    if not re.fullmatch(key_pattern, new_track.object_key):
        return _reject(IngestRejectReason.OBJECT_KEY_NOT_CONTENT_ADDRESSED)
    existing = tuple(existing_catalog)
    for track in existing:
        if track.track_id == new_track.track_id and track.file_sha256 != actual_sha256:
            return _reject(IngestRejectReason.TRACK_ID_CONFLICT)
    for track in existing:
        if track.file_sha256 == actual_sha256:
            return IngestDecision(IngestOutcome.DUPLICATE, existing_track_id=track.track_id)
    return IngestDecision(IngestOutcome.ACCEPT)
