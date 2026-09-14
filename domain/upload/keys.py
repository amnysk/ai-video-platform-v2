"""upload key（ADR-0020 §3）。予約台帳の ``idempotency_key`` に使う。

版・attempt・run・時刻を含めない。同じ完成動画を同じ投稿先へ送る限り同じキーになり、
``UNIQUE(idempotency_key)`` が二重投稿を DB で止める。
"""

from __future__ import annotations

import hashlib

from contracts.upload import UPLOAD_KEY_STAGE
from domain.artifact.hashing import canonical_json_bytes


def compute_upload_key(*, episode_id: str, final_video_sha256: str, destination_id: str) -> str:
    payload = {
        "stage": UPLOAD_KEY_STAGE,
        "episode_id": episode_id,
        "final_video_sha256": final_video_sha256,
        "destination": destination_id,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


__all__ = ["compute_upload_key"]
