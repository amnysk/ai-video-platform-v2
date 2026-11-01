"""地図の証跡（``GeoMapRecord``）の hash 結びつき（ADR-0084 §5、INV-141）。純関数。

結びつきの鎖: manifest ─(manifest_sha256)→ 検証 ─(verification_sha256)→ 画像 ─(png_sha256)→
クリップ ─(clip_sha256)→ 完成動画 ─(final_video_sha256)。どれか 1 つでも食い違えば
``BindingProblem`` を返す（空 = 鎖が閉じている）。再計算できるものは再計算し、記録の自己申告を
信じない。
"""

from __future__ import annotations

from enum import StrEnum

from contracts.geo import GeoVerdict
from contracts.geo_map import MapAccuracyResult, MapPresentationResult
from contracts.geo_scene import GeoMapRecord
from domain.geo.digest import sha256_of


class BindingProblem(StrEnum):
    MANIFEST_HASH = "manifest_hash"
    VERIFICATION_HASH = "verification_hash"
    NO_CLIP = "no_clip"
    NO_FINAL_VIDEO = "no_final_video"
    OTHER_FINAL_VIDEO = "other_final_video"


def verification_digest_of(
    accuracy: MapAccuracyResult, presentation: MapPresentationResult | None
) -> str:
    """``accuracy`` と ``presentation`` だけの指紋（クリップ・完成動画の追記で変わらない）。"""
    return sha256_of(
        {
            "accuracy": accuracy.model_dump(mode="json"),
            "presentation": None if presentation is None else presentation.model_dump(mode="json"),
        }
    )


def verification_digest(record: GeoMapRecord) -> str:
    return verification_digest_of(record.accuracy, record.presentation)


def evidence_binding_id(final_video_sha256: str, input_hash: str) -> str:
    """証跡の object key の末尾（完成動画と描画入力から導く。ゲートも同じ式で引く）。"""
    return sha256_of({"final_video_sha256": final_video_sha256, "input_hash": input_hash})


def record_sha256(record: GeoMapRecord) -> str:
    """証跡全体の指紋。品質レポートの ``evidence_ref`` に使う。"""
    return sha256_of(record)


def binding_problems(
    record: GeoMapRecord, *, final_video_sha256: str | None
) -> tuple[BindingProblem, ...]:
    """PASS の記録だけが「使われた地図」になれる。鎖の切れ目を全部返す。"""
    problems: list[BindingProblem] = []
    if record.manifest is not None and record.manifest_sha256 != sha256_of(record.manifest):
        problems.append(BindingProblem.MANIFEST_HASH)
    if record.manifest is None and record.manifest_sha256 is not None:
        problems.append(BindingProblem.MANIFEST_HASH)
    if record.verification_sha256 != verification_digest(record):
        problems.append(BindingProblem.VERIFICATION_HASH)
    if record.verdict is GeoVerdict.PASS:
        if record.clip_sha256 is None:
            problems.append(BindingProblem.NO_CLIP)
        if record.final_video_sha256 is None:
            problems.append(BindingProblem.NO_FINAL_VIDEO)
        elif final_video_sha256 is not None and record.final_video_sha256 != final_video_sha256:
            problems.append(BindingProblem.OTHER_FINAL_VIDEO)
    return tuple(problems)


__all__ = [
    "BindingProblem",
    "binding_problems",
    "evidence_binding_id",
    "record_sha256",
    "verification_digest",
    "verification_digest_of",
]
