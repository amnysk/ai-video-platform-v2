"""地図を有効にする前提の確認（ADR-0084 §9）。worker の組み立てで呼ぶ。"""

from __future__ import annotations

from infrastructure.config import Settings


def require_quality_gate(settings: Settings) -> None:
    """地図の BLOCK が投稿を止める仕組み（品質ゲートの強制）が無いまま地図を有効にしない。

    Render は拒否した地図を使わず従来の動画を残して完成動画を作る。誤った地図の拒否は品質ゲートが
    担うので、ゲートが ``enforce`` でなければ起動を止める（fail fast）。
    """
    if not settings.quality_gate_enforce:
        raise SystemExit(
            "GEO_MAPS_ENABLED requires the quality gate to be enforced "
            "(set AVP_QUALITY_GATE_ENFORCE=true): a refused map is stopped from upload by the gate"
        )


__all__ = ["require_quality_gate"]
