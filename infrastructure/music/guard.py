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


__all__ = ["require_description_composition_for_bgm"]
