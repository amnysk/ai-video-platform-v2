"""BGM を有効にする前提の確認（ADR-0086 §7）。render / upload worker の組み立てで呼ぶ。"""

from __future__ import annotations


def require_description_composition_for_bgm(*, bgm_on: bool, composition_enabled: bool) -> None:
    """説明欄の構成が無効だと ``build_upload_metadata`` は credits を捨てる。

    帰属が必要な曲がクレジット無しで公開されないよう、BGM ON で構成 OFF なら起動を止める。
    flag は各 ``run_worker`` が読んで渡す。
    """
    if bgm_on and not composition_enabled:
        raise SystemExit(
            "BGM_ENABLED requires AVP_UPLOAD_DESCRIPTION_COMPOSED=true: without it the BGM "
            "credit lines are dropped from the description"
        )


def require_quality_gate_enforce_for_bgm(*, bgm_on: bool, enforce_on: bool) -> None:
    """BGM の権利違反・音量の欠陥を止めるのは品質ゲートだけ（ADR-0087 §10 / INV-159）。

    ゲートが ``enforce`` でなければ BGM 付きの動画が検査なしで投稿されるので、起動を止める。
    """
    if bgm_on and not enforce_on:
        raise SystemExit(
            "BGM_ENABLED requires AVP_QUALITY_GATE_ENFORCE=true: without the enforced gate a "
            "video with BGM is not checked before upload"
        )


def require_bgm_for_acquisition(*, acquisition_on: bool, bgm_on: bool) -> None:
    """自動収集は BGM が有効なときだけ（ADR-0087 §10 / INV-159）。収集 worker の起動で呼ぶ。"""
    if acquisition_on and not bgm_on:
        raise SystemExit(
            "BGM_ACQUISITION_ENABLED requires BGM_ENABLED=true: acquired tracks are only usable "
            "(and checked) through the BGM path"
        )


__all__ = [
    "require_bgm_for_acquisition",
    "require_description_composition_for_bgm",
    "require_quality_gate_enforce_for_bgm",
]
