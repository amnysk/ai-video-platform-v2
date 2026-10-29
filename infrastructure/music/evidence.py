"""BGM の使用証跡の置き場と読み口（ADR-0086）。

render（書き手）と品質ゲート・説明欄（読み手）が使う唯一の key 式。

証跡の key は **完成動画の媒体 sha256 と描画の input_hash** から決まる（内容の hash ではない）。
内容の hash だと、gate は prefix 列挙なしには証跡を見つけられない。地図の証跡
（``infrastructure.geo.service.geo_evidence_key``）と同じ式で結び付ける。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from contracts.music_render import BGM_EVIDENCE_OBJECT_TYPE, BgmRenderEvidence
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from infrastructure.storage.artifact_store import ArtifactStore


class BgmEvidenceError(RuntimeError):
    """証跡が読めない・壊れている・別の動画のもの。「無い」とは区別し fail-closed にする。"""


def bgm_evidence_binding_id(media_sha256: str, input_hash: str) -> str:
    return sha256_hex(
        canonical_json_bytes({"final_video_sha256": media_sha256, "input_hash": input_hash})
    )


def bgm_evidence_key(episode_id: str, media_sha256: str, input_hash: str) -> str:
    return artifact_object_key(
        episode_id, BGM_EVIDENCE_OBJECT_TYPE, bgm_evidence_binding_id(media_sha256, input_hash)
    )


def evidence_digest(payload: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes(payload))


async def load_bgm_evidence(
    store: ArtifactStore, *, episode_id: str, media_sha256: str, input_hash: str
) -> tuple[BgmRenderEvidence, str] | None:
    """(証跡, その正準 sha256)。保存されていなければ ``None``（= その動画に BGM 証跡は無い）。

    あるのに読めない・検証に落ちる・別の episode / 入力 / 動画のものは ``BgmEvidenceError``。
    """
    key = bgm_evidence_key(episode_id, media_sha256, input_hash)
    try:
        payload = await store.get_json(key)
    except KeyError:
        return None
    except Exception as exc:  # noqa: BLE001 - 読めないものを「無い」にしない
        raise BgmEvidenceError(f"bgm evidence unreadable: {type(exc).__name__}") from exc
    try:
        evidence = BgmRenderEvidence.model_validate(payload)
    except ValueError as exc:
        raise BgmEvidenceError("bgm evidence does not satisfy its contract") from exc
    if (
        evidence.episode_id != episode_id
        or evidence.input_hash != input_hash
        or evidence.final_video_sha256 != media_sha256
    ):
        raise BgmEvidenceError("bgm evidence belongs to another video")
    return evidence, evidence_digest(payload)
