"""決定論的な検証と LLM / critic の評価の合流（INV-94、T32）。

critic の結論は**等しいか厳しい側にしか**動かせない。決定論的な BLOCK / REVIEW を critic が PASS に
しても、結果は変わらない。点数を verdict に変える経路は無い（``GeoVerdict`` だけ受け取る）。
"""

from __future__ import annotations

from contracts.geo import GeoCheck, GeoVerdict, GeoVerification, stricter

CRITIC_CHECK_NAME = "critic"


def combine_verdicts(deterministic: GeoVerification, critic: GeoVerdict) -> GeoVerification:
    if not isinstance(critic, GeoVerdict):
        raise TypeError("a critic can only contribute a GeoVerdict, never a score")
    check = GeoCheck(
        name=CRITIC_CHECK_NAME,
        outcome=critic,
        detail="critic verdict (advisory: it can only match or tighten the deterministic verdict)",
    )
    return GeoVerification(
        verdict=stricter(deterministic.verdict, critic),
        checks=(*deterministic.checks, check),
    )
